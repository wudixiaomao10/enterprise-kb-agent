from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

from backend.app.observability import (
    extract_usage,
    observed_span,
    record_embedding_call,
)
from backend.app.retrieval.embeddings import LOCAL_EMBEDDING_DIMENSIONS, local_embedding


DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_DIMENSIONS = 1536


@dataclass(frozen=True)
class EmbeddingProbe:
    provider: str
    model: str
    dimensions: int
    elapsed_ms: float
    vector_norm: float


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimensions: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_query(self, query: str) -> list[float]:
        ...

    def probe(self) -> EmbeddingProbe:
        ...


class LocalHashEmbeddingProvider:
    name = "local-hash"
    model = "local-hash-v1"
    dimensions = LOCAL_EMBEDDING_DIMENSIONS

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        started = time.perf_counter()
        try:
            with observed_span(
                "embedding.local",
                attributes={
                    "embedding.provider": self.name,
                    "embedding.model": self.model,
                    "embedding.count": len(texts),
                },
                stage="embedding",
            ):
                vectors = [local_embedding(text) for text in texts]
        except Exception:
            record_embedding_call(
                provider=self.name,
                model=self.model,
                operation="embed_texts",
                duration_ms=(time.perf_counter() - started) * 1000,
                status="error",
            )
            raise
        record_embedding_call(
            provider=self.name,
            model=self.model,
            operation="embed_texts",
            duration_ms=(time.perf_counter() - started) * 1000,
            input_tokens=0,
        )
        return vectors

    def embed_query(self, query: str) -> list[float]:
        return local_embedding(query)

    def probe(self) -> EmbeddingProbe:
        started = time.perf_counter()
        vector = self.embed_query("enterprise knowledge base embedding health check")
        return build_probe(self, vector, started)


class RemoteEmbeddingProvider:
    def __init__(
        self,
        *,
        name: str,
        model: str,
        dimensions: int,
        batch_size: int,
        client: Any,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("Embedding dimensions must be positive")
        if batch_size <= 0 or batch_size > 2048:
            raise ValueError("Embedding batch size must be between 1 and 2048")
        self.name = name
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self._client = client
        self.last_input_tokens = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise ValueError("Embedding input cannot contain empty text")

        vectors: list[list[float]] = []
        total_tokens = 0
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            started = time.perf_counter()
            try:
                with observed_span(
                    "embedding.remote",
                    attributes={
                        "embedding.provider": self.name,
                        "embedding.model": self.model,
                        "embedding.batch_size": len(batch),
                    },
                    stage="embedding",
                ) as span:
                    response = self._client.embeddings.create(
                        model=self.model,
                        input=batch,
                        dimensions=self.dimensions,
                        encoding_format="float",
                    )
                    usage = extract_usage(response)
                    batch_tokens = usage["input_tokens"] or usage["total_tokens"]
                    if span is not None:
                        span.set_attribute("embedding.input_tokens", batch_tokens)
                ordered = sorted(response.data, key=lambda item: item.index)
                batch_vectors = [list(item.embedding) for item in ordered]
                validate_embedding_batch(batch, batch_vectors, self.dimensions)
            except Exception:
                record_embedding_call(
                    provider=self.name,
                    model=self.model,
                    operation="embed_texts",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    status="error",
                )
                raise
            vectors.extend(batch_vectors)
            total_tokens += batch_tokens
            record_embedding_call(
                provider=self.name,
                model=self.model,
                operation="embed_texts",
                duration_ms=(time.perf_counter() - started) * 1000,
                input_tokens=batch_tokens,
            )

        self.last_input_tokens = total_tokens
        return vectors

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0]

    def probe(self) -> EmbeddingProbe:
        started = time.perf_counter()
        vector = self.embed_query("企业知识库真实 embedding 健康检查")
        return build_probe(self, vector, started)


class OpenAIEmbeddingProvider(RemoteEmbeddingProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        dimensions: int = DEFAULT_OPENAI_DIMENSIONS,
        batch_size: int = 64,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError(
                    "OpenAI SDK is required for KNOWLEDGE_EMBEDDING_PROVIDER=openai"
                ) from error

            client_args: dict[str, Any] = {
                "api_key": api_key,
                "timeout": timeout_seconds,
                "max_retries": max_retries,
            }
            if base_url:
                client_args["base_url"] = base_url.rstrip("/")
            client = OpenAI(**client_args)

        super().__init__(
            name=f"openai:{model}",
            model=model,
            dimensions=dimensions,
            batch_size=batch_size,
            client=client,
        )


class AzureOpenAIEmbeddingProvider(RemoteEmbeddingProvider):
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        api_version: str,
        deployment: str,
        dimensions: int = DEFAULT_OPENAI_DIMENSIONS,
        batch_size: int = 64,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                from openai import AzureOpenAI
            except ImportError as error:
                raise RuntimeError(
                    "OpenAI SDK is required for Azure OpenAI embeddings"
                ) from error

            client = AzureOpenAI(
                api_key=api_key,
                azure_endpoint=endpoint.rstrip("/"),
                api_version=api_version,
                timeout=timeout_seconds,
                max_retries=max_retries,
            )

        super().__init__(
            name=f"azure-openai:{deployment}",
            model=deployment,
            dimensions=dimensions,
            batch_size=batch_size,
            client=client,
        )


def create_embedding_provider() -> EmbeddingProvider:
    load_dotenv_if_available()
    provider = os.getenv("KNOWLEDGE_EMBEDDING_PROVIDER", "local").strip().lower()
    if provider in {"local", "local-hash", "hash"}:
        return LocalHashEmbeddingProvider()

    dimensions = env_int(
        "KNOWLEDGE_EMBEDDING_DIMENSIONS",
        DEFAULT_OPENAI_DIMENSIONS,
    )
    batch_size = env_int("KNOWLEDGE_EMBEDDING_BATCH_SIZE", 64)
    timeout_seconds = env_float("KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS", 30.0)
    max_retries = env_int("KNOWLEDGE_EMBEDDING_MAX_RETRIES", 3)

    if provider == "openai":
        api_key = require_env("OPENAI_API_KEY", provider)
        model = os.getenv(
            "KNOWLEDGE_EMBEDDING_MODEL",
            os.getenv("OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        )
        return OpenAIEmbeddingProvider(
            api_key=api_key,
            model=model,
            base_url=os.getenv("OPENAI_BASE_URL"),
            dimensions=dimensions,
            batch_size=batch_size,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )

    if provider in {"azure", "azure-openai"}:
        return AzureOpenAIEmbeddingProvider(
            api_key=require_env("AZURE_OPENAI_API_KEY", provider),
            endpoint=require_env("AZURE_OPENAI_ENDPOINT", provider),
            api_version=require_env("AZURE_OPENAI_API_VERSION", provider),
            deployment=require_env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", provider),
            dimensions=dimensions,
            batch_size=batch_size,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )

    raise RuntimeError(
        "Unsupported KNOWLEDGE_EMBEDDING_PROVIDER. "
        "Use local, openai, or azure-openai."
    )


def validate_embedding_batch(
    inputs: list[str],
    vectors: list[list[float]],
    dimensions: int,
) -> None:
    if len(inputs) != len(vectors):
        raise RuntimeError(
            f"Embedding provider returned {len(vectors)} vectors for {len(inputs)} inputs"
        )
    for index, vector in enumerate(vectors):
        if len(vector) != dimensions:
            raise RuntimeError(
                f"Embedding vector {index} has {len(vector)} dimensions; "
                f"expected {dimensions}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError(f"Embedding vector {index} contains non-finite values")


def build_probe(
    provider: EmbeddingProvider,
    vector: list[float],
    started: float,
) -> EmbeddingProbe:
    validate_embedding_batch(["probe"], [vector], provider.dimensions)
    return EmbeddingProbe(
        provider=provider.name,
        model=provider.model,
        dimensions=len(vector),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        vector_norm=round(math.sqrt(sum(value * value for value in vector)), 6),
    )


def require_env(name: str, provider: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(
            f"{name} is required when KNOWLEDGE_EMBEDDING_PROVIDER={provider}"
        )
    return value.strip()


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)
    load_dotenv(".env.knowledge", override=False)
