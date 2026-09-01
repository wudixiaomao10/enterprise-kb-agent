from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.llm.providers import OpenAIJSONGenerationProvider
from backend.app.observability import (
    ObservabilityConfig,
    correlation_context,
    current_context,
    extract_usage,
    metrics_snapshot,
    observed_span,
    otlp_signal_endpoint,
    record_llm_call,
    reset_metrics_for_tests,
)
from backend.app.retrieval.providers import OpenAIEmbeddingProvider


class _FakeChatCompletions:
    def create(self, **_: object) -> object:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"summary":"ok","claims":[]}')
                )
            ],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4, total_tokens=16),
        )


class _FakeEmbeddings:
    def create(self, **kwargs: object) -> object:
        return SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[1.0, 0.0, 0.0])],
            usage=SimpleNamespace(total_tokens=8),
        )


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_metrics_for_tests()

    def test_extract_usage_supports_openai_attributes_and_dicts(self) -> None:
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150)
        )
        self.assertEqual(
            extract_usage(response),
            {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        )
        self.assertEqual(
            extract_usage({"input_tokens": 7, "output_tokens": 3}),
            {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )

    def test_cost_and_token_metrics_use_low_cardinality_attributes(self) -> None:
        with patch.dict(
            os.environ,
            {
                "KNOWLEDGE_LLM_INPUT_COST_PER_1K_USD": "2",
                "KNOWLEDGE_LLM_OUTPUT_COST_PER_1K_USD": "4",
            },
            clear=False,
        ):
            cost = record_llm_call(
                provider="openai",
                model="gpt-test",
                operation="knowledge_claims",
                duration_ms=12.5,
                input_tokens=1500,
                output_tokens=500,
            )

        self.assertEqual(cost, 5.0)
        snapshot = metrics_snapshot()
        call = next(item for item in snapshot["counters"] if item["name"] == "knowledge.llm.calls")
        self.assertEqual(call["attributes"], {
            "model": "gpt-test",
            "operation": "knowledge_claims",
            "provider": "openai",
            "status": "ok",
        })
        token_values = {
            item["attributes"]["token_type"]: item["value"]
            for item in snapshot["counters"]
            if item["name"] == "knowledge.llm.tokens"
        }
        self.assertEqual(token_values, {"input": 1500.0, "output": 500.0})
        cost_metric = next(item for item in snapshot["counters"] if item["name"] == "knowledge.llm.cost")
        self.assertEqual(cost_metric["value"], 5.0)
        latency = next(item for item in snapshot["histograms"] if item["name"] == "knowledge.llm.duration")
        self.assertEqual(latency["count"], 1)

    def test_context_and_span_record_correlation_without_payloads(self) -> None:
        with correlation_context(
            request_id="req_test",
            query_id="query_test",
            run_id="run_test",
            job_id="job_test",
        ):
            self.assertEqual(
                current_context(),
                {
                    "request_id": "req_test",
                    "query_id": "query_test",
                    "run_id": "run_test",
                    "job_id": "job_test",
                },
            )
            with observed_span("test.operation", stage="test"):
                pass

        self.assertEqual(current_context(), {})
        stage = next(item for item in metrics_snapshot()["histograms"] if item["name"] == "knowledge.stage.duration")
        self.assertEqual(stage["attributes"], {"stage": "test", "status": "ok"})

    def test_otel_config_and_endpoint_normalization(self) -> None:
        with patch.dict(
            os.environ,
            {
                "KNOWLEDGE_OTEL_ENABLED": "1",
                "KNOWLEDGE_OTEL_EXPORTER": "otlp",
                "KNOWLEDGE_OTEL_ENDPOINT": "http://collector:4318",
                "KNOWLEDGE_OTEL_SAMPLE_RATIO": "0.25",
            },
            clear=False,
        ):
            config = ObservabilityConfig.from_env()

        self.assertTrue(config.enabled)
        self.assertEqual(config.sample_ratio, 0.25)
        self.assertEqual(
            otlp_signal_endpoint(config.endpoint or "", "traces"),
            "http://collector:4318/v1/traces",
        )
        self.assertEqual(
            otlp_signal_endpoint("http://collector:4318/v1/metrics", "metrics"),
            "http://collector:4318/v1/metrics",
        )

    def test_openai_provider_usage_is_recorded(self) -> None:
        llm = OpenAIJSONGenerationProvider(
            client=SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions())),
            model="gpt-test",
            name="openai:gpt-test",
        )
        result = llm.generate_json(
            schema_name="test_schema",
            schema={"type": "object"},
            system_prompt="Return JSON.",
            user_payload={"question": "test"},
        )
        self.assertEqual(result["summary"], "ok")
        token_values = {
            item["attributes"]["token_type"]: item["value"]
            for item in metrics_snapshot()["counters"]
            if item["name"] == "knowledge.llm.tokens"
        }
        self.assertEqual(token_values, {"input": 12.0, "output": 4.0})

    def test_embedding_provider_usage_is_recorded(self) -> None:
        provider = OpenAIEmbeddingProvider(
            api_key="test",
            model="embedding-test",
            dimensions=3,
            client=SimpleNamespace(embeddings=_FakeEmbeddings()),
        )
        self.assertEqual(provider.embed_texts(["hello"]), [[1.0, 0.0, 0.0]])
        metric = next(
            item
            for item in metrics_snapshot()["counters"]
            if item["name"] == "knowledge.embedding.tokens"
        )
        self.assertEqual(metric["value"], 8.0)


if __name__ == "__main__":
    unittest.main()
