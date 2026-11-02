"""
OpenTelemetry tracing — scoped to the payment-execution path only
(PayIt, the guardrail engine, the mock processor, TrackIt).
Unlike config/llm.py's hard stop, this is soft-fail by design: a POC
should run identically whether or not a collector (e.g. Arize Phoenix)
is listening. The OTel SDK's default global tracer provider is already
a no-op, so get_tracer() is always safe to call even if init_tracing()
was never invoked or failed.
"""
import os
from typing import Optional

DEFAULT_ENDPOINT = "http://localhost:6006/v1/traces"


def init_tracing() -> tuple[bool, Optional[str]]:
    """Set up the OTLP exporter. Never raises — returns (enabled, endpoint).
    OTEL_SDK_DISABLED=true (the standard OpenTelemetry switch) turns it off, e.g. on a
    cloud host with no collector; get_tracer() then hands out no-op tracers."""
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False, None
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT)
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider = TracerProvider(
            resource=Resource.create({"service.name": "agentic-commerce-poc"})
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        return True, endpoint
    except Exception as exc:
        print(f"[Observability] Tracing disabled ({exc})")
        return False, None


def get_tracer(name: str):
    """Always returns a usable tracer — a real one if init_tracing() succeeded, otherwise a no-op."""
    from opentelemetry import trace
    return trace.get_tracer(name)
