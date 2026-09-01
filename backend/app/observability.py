from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


logger = logging.getLogger(__name__)

try:  # OpenTelemetry is optional for offline/unit-test environments.
    from opentelemetry import metrics as otel_metrics
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import Status, StatusCode

    OTEL_API_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when optional deps are absent
    otel_metrics = None
    otel_trace = None
    Status = None
    StatusCode = None
    OTEL_API_AVAILABLE = False


_ID_PATTERN = re.compile(r"[^A-Za-z0-9_./:-]+")
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "knowledge_request_id", default=None
)
_query_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "knowledge_query_id", default=None
)
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "knowledge_run_id", default=None
)
_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "knowledge_job_id", default=None
)


@dataclass(frozen=True)
class ObservabilityConfig:
    enabled: bool
    service_name: str
    service_version: str
    exporter: str
    endpoint: str | None
    sample_ratio: float
    metric_export_interval_ms: int

    @classmethod
    def from_env(cls) -> "ObservabilityConfig":
        endpoint = (
            os.getenv("KNOWLEDGE_OTEL_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            or None
        )
        exporter = os.getenv(
            "KNOWLEDGE_OTEL_EXPORTER",
            "otlp" if endpoint else "none",
        ).strip().lower()
        if exporter not in {"none", "console", "otlp"}:
            raise ValueError("KNOWLEDGE_OTEL_EXPORTER must be none, console, or otlp")
        return cls(
            enabled=env_bool("KNOWLEDGE_OTEL_ENABLED", False),
            service_name=os.getenv(
                "KNOWLEDGE_OTEL_SERVICE_NAME", "enterprise-kb-agent"
            ).strip()
            or "enterprise-kb-agent",
            service_version=os.getenv("KNOWLEDGE_OTEL_SERVICE_VERSION", "0.1.0").strip()
            or "0.1.0",
            exporter=exporter,
            endpoint=endpoint.rstrip("/") if endpoint else None,
            sample_ratio=max(0.0, min(env_float("KNOWLEDGE_OTEL_SAMPLE_RATIO", 1.0), 1.0)),
            metric_export_interval_ms=max(
                1000,
                env_int("KNOWLEDGE_OTEL_METRIC_EXPORT_INTERVAL_MS", 60000),
            ),
        )


class _InMemoryMetrics:
    """Small local accumulator used for diagnostics and dependency-free tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], list[float]
        ] = {}

    def add_counter(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, object],
    ) -> None:
        key = metric_key(name, attributes)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, object],
    ) -> None:
        key = metric_key(name, attributes)
        with self._lock:
            self._histograms.setdefault(key, []).append(value)

    def snapshot(self) -> dict[str, list[dict[str, object]]]:
        with self._lock:
            counters = [
                {
                    "name": name,
                    "attributes": dict(attributes),
                    "value": round(value, 6),
                }
                for (name, attributes), value in sorted(self._counters.items())
            ]
            histograms = []
            for (name, attributes), values in sorted(self._histograms.items()):
                histograms.append(
                    {
                        "name": name,
                        "attributes": dict(attributes),
                        "count": len(values),
                        "sum": round(sum(values), 6),
                        "min": round(min(values), 6),
                        "max": round(max(values), 6),
                    }
                )
            return {"counters": counters, "histograms": histograms}

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


_fallback_metrics = _InMemoryMetrics()
_config: ObservabilityConfig | None = None
_configured = False
_otel_ready = False
_otel_meter: Any | None = None
_otel_tracer: Any | None = None
_otel_instruments: dict[tuple[str, str], Any] = {}


def configure_observability(
    config: ObservabilityConfig | None = None,
) -> ObservabilityConfig:
    """Configure process-wide OTel providers once and always keep local metrics."""

    global _config, _configured, _otel_ready, _otel_meter, _otel_tracer
    if _configured:
        return _config or ObservabilityConfig.from_env()

    try:
        _config = config or load_config()
    except ValueError as error:
        logger.warning("Invalid observability configuration: %s", type(error).__name__)
        _config = ObservabilityConfig(
            enabled=False,
            service_name="enterprise-kb-agent",
            service_version="0.1.0",
            exporter="none",
            endpoint=None,
            sample_ratio=1.0,
            metric_export_interval_ms=60000,
        )
    _configured = True
    if not _config.enabled:
        return _config
    if not OTEL_API_AVAILABLE:
        logger.warning("OpenTelemetry SDK is not installed; telemetry is disabled")
        return _config

    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        resource = Resource.create(
            {
                "service.name": _config.service_name,
                "service.version": _config.service_version,
            }
        )
        tracer_provider = TracerProvider(
            resource=resource,
            sampler=TraceIdRatioBased(_config.sample_ratio),
        )
        metric_readers = []
        if _config.exporter == "console":
            from opentelemetry.sdk.metrics.export import (
                ConsoleMetricExporter,
                PeriodicExportingMetricReader,
            )
            from opentelemetry.sdk.trace.export import (
                ConsoleSpanExporter,
                SimpleSpanProcessor,
            )

            tracer_provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter())
            )
            metric_readers.append(
                PeriodicExportingMetricReader(
                    ConsoleMetricExporter(),
                    export_interval_millis=_config.metric_export_interval_ms,
                )
            )
        elif _config.exporter == "otlp":
            if not _config.endpoint:
                raise RuntimeError(
                    "KNOWLEDGE_OTEL_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT "
                    "is required for the otlp exporter"
                )
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=otlp_signal_endpoint(_config.endpoint, "traces")
                    )
                )
            )
            metric_readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=otlp_signal_endpoint(_config.endpoint, "metrics")
                    ),
                    export_interval_millis=_config.metric_export_interval_ms,
                )
            )

        otel_trace.set_tracer_provider(tracer_provider)
        otel_metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=metric_readers)
        )
        _otel_tracer = otel_trace.get_tracer(_config.service_name)
        _otel_meter = otel_metrics.get_meter(_config.service_name)
        _otel_ready = True
    except Exception as error:  # telemetry must never prevent the API from booting
        logger.warning("OpenTelemetry setup failed: %s", type(error).__name__)
    return _config


def load_config() -> ObservabilityConfig:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
        load_dotenv(".env.knowledge", override=False)
    except ImportError:
        pass
    return ObservabilityConfig.from_env()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@contextlib.contextmanager
def correlation_context(
    *,
    request_id: str | None = None,
    query_id: str | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
) -> Iterator[None]:
    tokens = []
    values = (
        (_request_id, request_id),
        (_query_id, query_id),
        (_run_id, run_id),
        (_job_id, job_id),
    )
    for variable, value in values:
        if value is not None:
            tokens.append((variable, variable.set(value)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def set_context_value(name: str, value: str | None) -> None:
    variables = {
        "request_id": _request_id,
        "query_id": _query_id,
        "run_id": _run_id,
        "job_id": _job_id,
    }
    variable = variables.get(name)
    if variable is None:
        raise ValueError(f"Unsupported observability context value: {name}")
    variable.set(value)


def current_context() -> dict[str, str]:
    values = {
        "request_id": _request_id.get(),
        "query_id": _query_id.get(),
        "run_id": _run_id.get(),
        "job_id": _job_id.get(),
    }
    return {key: value for key, value in values.items() if value}


@contextlib.contextmanager
def observed_span(
    name: str,
    *,
    attributes: Mapping[str, object] | None = None,
    stage: str | None = None,
) -> Iterator[Any | None]:
    started = time.perf_counter()
    active_span = None
    status = "ok"
    span_attributes = {
        **{
            f"correlation.{key}": value
            for key, value in current_context().items()
        },
        **safe_attributes(attributes),
    }
    span_context = (
        _otel_tracer.start_as_current_span(name, attributes=span_attributes)
        if _otel_ready and _otel_tracer is not None
        else contextlib.nullcontext(None)
    )
    try:
        with span_context as active_span:
            yield active_span
    except BaseException as error:
        status = "error"
        if active_span is not None:
            active_span.set_attribute("error.type", type(error).__name__)
            if Status is not None and StatusCode is not None:
                active_span.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        record_stage_latency(
            stage or name,
            (time.perf_counter() - started) * 1000,
            status=status,
        )


def record_http_request(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_ms: float,
) -> None:
    attrs = {
        "method": metric_label(method),
        "route": metric_label(route),
        "status_code": str(status_code),
    }
    record_counter("knowledge.http.requests", 1, attrs, unit="{request}")
    record_histogram("knowledge.http.duration", duration_ms, attrs, unit="ms")


def record_stage_latency(stage: str, duration_ms: float, *, status: str = "ok") -> None:
    record_histogram(
        "knowledge.stage.duration",
        duration_ms,
        {"stage": metric_label(stage), "status": metric_label(status)},
        unit="ms",
    )


def record_llm_call(
    *,
    provider: str,
    model: str,
    operation: str,
    duration_ms: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    status: str = "ok",
    input_cost_per_1k_usd: float | None = None,
    output_cost_per_1k_usd: float | None = None,
) -> float:
    attrs = {
        "provider": metric_label(provider),
        "model": metric_label(model),
        "operation": metric_label(operation),
        "status": metric_label(status),
    }
    input_tokens = max(int(input_tokens), 0)
    output_tokens = max(int(output_tokens), 0)
    input_rate = (
        configured_cost("llm", "input", model)
        if input_cost_per_1k_usd is None
        else max(float(input_cost_per_1k_usd), 0.0)
    )
    output_rate = (
        configured_cost("llm", "output", model)
        if output_cost_per_1k_usd is None
        else max(float(output_cost_per_1k_usd), 0.0)
    )
    cost = (input_tokens / 1000 * input_rate) + (
        output_tokens / 1000 * output_rate
    )
    record_counter("knowledge.llm.calls", 1, attrs, unit="{call}")
    record_counter(
        "knowledge.llm.tokens",
        input_tokens,
        {**attrs, "token_type": "input"},
        unit="{token}",
    )
    record_counter(
        "knowledge.llm.tokens",
        output_tokens,
        {**attrs, "token_type": "output"},
        unit="{token}",
    )
    record_counter("knowledge.llm.cost", cost, attrs, unit="USD")
    record_histogram("knowledge.llm.duration", duration_ms, attrs, unit="ms")
    return cost


def record_embedding_call(
    *,
    provider: str,
    model: str,
    operation: str,
    duration_ms: float,
    input_tokens: int = 0,
    status: str = "ok",
    input_cost_per_1k_usd: float | None = None,
) -> float:
    attrs = {
        "provider": metric_label(provider),
        "model": metric_label(model),
        "operation": metric_label(operation),
        "status": metric_label(status),
    }
    input_tokens = max(int(input_tokens), 0)
    rate = (
        configured_cost("embedding", "input", model)
        if input_cost_per_1k_usd is None
        else max(float(input_cost_per_1k_usd), 0.0)
    )
    cost = input_tokens / 1000 * rate
    record_counter("knowledge.embedding.calls", 1, attrs, unit="{call}")
    record_counter("knowledge.embedding.tokens", input_tokens, attrs, unit="{token}")
    record_counter("knowledge.embedding.cost", cost, attrs, unit="USD")
    record_histogram("knowledge.embedding.duration", duration_ms, attrs, unit="ms")
    return cost


def record_counter(
    name: str,
    value: float,
    attributes: Mapping[str, object],
    *,
    unit: str = "",
) -> None:
    normalized = safe_attributes(attributes)
    _fallback_metrics.add_counter(name, float(value), normalized)
    if _otel_ready and _otel_meter is not None:
        instrument = get_instrument("counter", name, unit, normalized)
        instrument.add(float(value), attributes=normalized)


def record_histogram(
    name: str,
    value: float,
    attributes: Mapping[str, object],
    *,
    unit: str = "",
) -> None:
    normalized = safe_attributes(attributes)
    _fallback_metrics.observe(name, float(value), normalized)
    if _otel_ready and _otel_meter is not None:
        instrument = get_instrument("histogram", name, unit, normalized)
        instrument.record(float(value), attributes=normalized)


def get_instrument(kind: str, name: str, unit: str, attributes: Mapping[str, str]) -> Any:
    key = (kind, name)
    instrument = _otel_instruments.get(key)
    if instrument is not None:
        return instrument
    description = f"{name} for enterprise knowledge base observability"
    if kind == "counter":
        instrument = _otel_meter.create_counter(name, unit=unit, description=description)
    else:
        instrument = _otel_meter.create_histogram(
            name,
            unit=unit,
            description=description,
        )
    _otel_instruments[key] = instrument
    return instrument


def metrics_snapshot() -> dict[str, list[dict[str, object]]]:
    return _fallback_metrics.snapshot()


def reset_metrics_for_tests() -> None:
    _fallback_metrics.clear()


def extract_usage(response_or_usage: Any) -> dict[str, int]:
    usage = getattr(response_or_usage, "usage", response_or_usage)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def read(*names: str) -> int:
        for name in names:
            if isinstance(usage, Mapping):
                value = usage.get(name)
            else:
                value = getattr(usage, name, None)
            if value is not None:
                try:
                    return max(int(value), 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    input_tokens = read("prompt_tokens", "input_tokens")
    output_tokens = read("completion_tokens", "output_tokens")
    total_tokens = read("total_tokens") or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def configured_cost(kind: str, token_type: str, model: str) -> float:
    pricing = os.getenv("KNOWLEDGE_MODEL_PRICING_JSON", "").strip()
    if pricing:
        try:
            data = json.loads(pricing)
            model_data = data.get(model) or data.get("default") or {}
            value = model_data.get(token_type)
            if value is not None:
                return max(float(value), 0.0)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Ignoring invalid KNOWLEDGE_MODEL_PRICING_JSON")
    key = f"KNOWLEDGE_{kind.upper()}_{token_type.upper()}_COST_PER_1K_USD"
    try:
        return max(float(os.getenv(key, "0")), 0.0)
    except ValueError:
        return 0.0


def observability_status() -> dict[str, object]:
    config = _config or load_config()
    return {
        "enabled": config.enabled,
        "sdk_available": OTEL_API_AVAILABLE,
        "otel_ready": _otel_ready,
        "service_name": config.service_name,
        "exporter": config.exporter,
        "endpoint_configured": bool(config.endpoint),
        "sample_ratio": config.sample_ratio,
    }


def safe_attributes(attributes: Mapping[str, object] | None) -> dict[str, str | int | float | bool]:
    if not attributes:
        return {}
    safe: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, bool | int | float):
            safe[str(key)] = value
        else:
            safe[str(key)] = metric_label(str(value))
    return safe


def metric_key(
    name: str,
    attributes: Mapping[str, object],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    return name, tuple(
        sorted((str(key), str(value)) for key, value in safe_attributes(attributes).items())
    )


def metric_label(value: str, max_length: int = 96) -> str:
    normalized = _ID_PATTERN.sub("_", str(value).strip())
    return normalized[:max_length] or "unknown"


def otlp_signal_endpoint(base: str, signal: str) -> str:
    suffix = f"/v1/{signal}"
    return base if base.endswith(suffix) else f"{base.rstrip('/')}{suffix}"


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


__all__ = [
    "OTEL_API_AVAILABLE",
    "ObservabilityConfig",
    "configure_observability",
    "configured_cost",
    "correlation_context",
    "current_context",
    "extract_usage",
    "metrics_snapshot",
    "new_id",
    "observability_status",
    "observed_span",
    "record_embedding_call",
    "record_http_request",
    "record_llm_call",
    "record_stage_latency",
    "reset_metrics_for_tests",
    "safe_attributes",
    "set_context_value",
]
