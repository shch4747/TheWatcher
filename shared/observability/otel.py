"""OpenTelemetry tracing (ADR-0013).

The SQLite tables answer "what did last week cost". This answers "why
did *this* batch cost that" - per-call spans with the prompt, the
reply, token counts and latency, nested under the agent run.

Almost free to wire: every model call in this app already goes through a
pydantic-ai `Agent`, and pydantic-ai ships native instrumentation
emitting OpenTelemetry GenAI semantic-convention spans. So this module
is just a TracerProvider, an OTLP exporter, and `Agent.instrument_all`.

The sink is Arize Phoenix in docker-compose (one container, SQLite,
OTLP in), but nothing here is Phoenix-specific - point
`OTEL_ENDPOINT` at any OTLP/HTTP collector.
"""
from __future__ import annotations

import logging

from shared.config import settings

logger = logging.getLogger(__name__)

_configured = False


def setup_tracing() -> bool:
    """Called once from the composition root. Returns whether tracing
    was actually enabled.

    Never raises: the app must start and ingest normally when the trace
    backend is missing, unreachable or the packages aren't installed -
    observability is not allowed to be a hard dependency of the thing it
    observes.
    """
    global _configured
    if _configured:
        return True
    if not settings.otel_enabled or not settings.otel_endpoint:
        logger.info("tracing disabled (OTEL_ENABLED=%s)", settings.otel_enabled)
        return False

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings

        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.otel_service_name})
        )
        # Batched and off the request path: a slow or dead collector
        # costs a background flush, not ingest latency.
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
        )
        Agent.instrument_all(
            InstrumentationSettings(
                tracer_provider=provider,
                # Prompts and WhatsApp message text in spans. The point
                # of tracing, and consistent with ADR-0012's keep-the-PII
                # stance for this internal deployment - but it does mean
                # member content reaches the trace store, so it's a
                # setting rather than a hardcoded True.
                include_content=settings.otel_include_content,
            )
        )
    except Exception:  # noqa: BLE001 - missing packages, bad endpoint, anything
        logger.exception("tracing setup failed; continuing without traces")
        return False

    _configured = True
    logger.info("tracing -> %s (content=%s)", settings.otel_endpoint, settings.otel_include_content)
    return True
