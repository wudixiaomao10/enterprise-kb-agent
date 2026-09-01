from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Protocol

from backend.app.observability import (
    extract_usage,
    observed_span,
    record_llm_call,
)
from backend.app.retrieval.providers import (
    env_float,
    env_int,
    load_dotenv_if_available,
    require_env,
)


class JSONGenerationProvider(Protocol):
    name: str
    model: str

    def generate_json(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class OpenAIJSONGenerationProvider:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        name: str,
        json_mode: str = "json_schema",
        allow_json_object_fallback: bool = True,
    ) -> None:
        if json_mode not in {"json_schema", "json_object"}:
            raise ValueError("json_mode must be json_schema or json_object")
        self._client = client
        self.model = model
        self.name = name
        self.json_mode = json_mode
        self.allow_json_object_fallback = allow_json_object_fallback

    def generate_json(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        response_format = build_response_format(self.json_mode, schema_name, schema)
        try:
            response = self._complete(
                system_prompt,
                user_payload,
                response_format,
                operation=schema_name,
            )
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            can_fallback = (
                self.json_mode == "json_schema"
                and self.allow_json_object_fallback
                and status_code in {400, 404, 422}
            )
            if not can_fallback:
                raise
            response = self._complete(
                system_prompt,
                user_payload,
                {"type": "json_object"},
                operation=schema_name,
            )

        content = response.choices[0].message.content or ""
        data = parse_json_object(content)
        if not isinstance(data, dict):
            raise RuntimeError("Structured model output must be a JSON object")
        return data

    def _complete(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        response_format: dict[str, Any],
        *,
        operation: str,
    ) -> Any:
        started = time.perf_counter()
        try:
            with observed_span(
                "llm.chat.completions",
                attributes={
                    "llm.provider": self.name,
                    "llm.model": self.model,
                    "llm.operation": operation,
                },
                stage="llm",
            ) as span:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": json.dumps(user_payload, ensure_ascii=False),
                        },
                    ],
                    response_format=response_format,
                    temperature=0,
                )
                usage = extract_usage(response)
                if span is not None:
                    span.set_attributes(
                        {
                            "llm.input_tokens": usage["input_tokens"],
                            "llm.output_tokens": usage["output_tokens"],
                            "llm.total_tokens": usage["total_tokens"],
                        }
                    )
        except Exception:
            record_llm_call(
                provider=self.name,
                model=self.model,
                operation=operation,
                duration_ms=(time.perf_counter() - started) * 1000,
                status="error",
            )
            raise

        record_llm_call(
            provider=self.name,
            model=self.model,
            operation=operation,
            duration_ms=(time.perf_counter() - started) * 1000,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        return response


def create_json_llm() -> JSONGenerationProvider | None:
    load_dotenv_if_available()
    provider = os.getenv("KNOWLEDGE_LLM_PROVIDER", "none").strip().lower()
    if provider in {"none", "off", "disabled", "local"}:
        return None

    timeout_seconds = env_float("KNOWLEDGE_LLM_TIMEOUT_SECONDS", 45.0)
    max_retries = env_int("KNOWLEDGE_LLM_MAX_RETRIES", 2)
    json_mode = os.getenv("KNOWLEDGE_LLM_JSON_MODE", "json_schema").strip().lower()
    allow_fallback = os.getenv(
        "KNOWLEDGE_LLM_ALLOW_JSON_OBJECT_FALLBACK", "1"
    ).strip().lower() not in {"0", "false", "no"}

    if provider == "openai":
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("OpenAI SDK is required for structured generation") from error
        model = os.getenv("KNOWLEDGE_LLM_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
        client_args: dict[str, Any] = {
            "api_key": require_env("OPENAI_API_KEY", provider),
            "timeout": timeout_seconds,
            "max_retries": max_retries,
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            client_args["base_url"] = base_url.rstrip("/")
        client = OpenAI(**client_args)
        return OpenAIJSONGenerationProvider(
            client=client,
            model=model,
            name=f"openai:{model}",
            json_mode=json_mode,
            allow_json_object_fallback=allow_fallback,
        )

    if provider in {"azure", "azure-openai"}:
        try:
            from openai import AzureOpenAI
        except ImportError as error:
            raise RuntimeError("OpenAI SDK is required for Azure structured generation") from error
        deployment = require_env("AZURE_OPENAI_LLM_DEPLOYMENT", provider)
        client = AzureOpenAI(
            api_key=require_env("AZURE_OPENAI_API_KEY", provider),
            azure_endpoint=require_env("AZURE_OPENAI_ENDPOINT", provider),
            api_version=require_env("AZURE_OPENAI_API_VERSION", provider),
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        return OpenAIJSONGenerationProvider(
            client=client,
            model=deployment,
            name=f"azure-openai:{deployment}",
            json_mode=json_mode,
            allow_json_object_fallback=allow_fallback,
        )

    raise RuntimeError(
        "Unsupported KNOWLEDGE_LLM_PROVIDER. Use none, openai, or azure-openai."
    )


def build_response_format(
    mode: str,
    schema_name: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    if mode == "json_object":
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "strict": True,
            "schema": schema,
        },
    }


def parse_json_object(content: str) -> Any:
    stripped = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    return json.loads(stripped)
