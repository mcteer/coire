"""OpenTelemetry wiring (FR-014).

Everything exports to the local collector on core and stays there — no telemetry egresses
(research R11). Exporter failures are logged, never raised: a collector outage must not turn
into a request failure (contracts/compose-topology.md — nothing depends on the collector).
"""

from __future__ import annotations

import logging

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)
_configured = False
NETWORK_PATH_ATTRIBUTE = "network.path"
NETWORK_PEER_ATTRIBUTE = "network.peer"
SENSITIVE_FIELD_FRAGMENTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "prompt",
    "response",
)


class TelemetryRedactionFilter(logging.Filter):
    """Defense in depth for structured extras; application code must still avoid content."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("opentelemetry.exporter"):
            return False
        for key in tuple(record.__dict__):
            if any(fragment in key.lower() for fragment in SENSITIVE_FIELD_FRAGMENTS):
                record.__dict__[key] = "[REDACTED]"
        return True


def configure_telemetry(service_name: str, service_version: str, endpoint: str) -> None:
    """Install tracer and meter providers exporting OTLP to the local collector.

    Idempotent, and best-effort: if the collector is unreachable the SDK buffers and drops
    rather than raising, which is what keeps FR-014 from becoming an availability risk.
    """
    global _configured
    if _configured:
        return

    resource = Resource.create({"service.name": service_name, "service.version": service_version})
    try:
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True), export_interval_millis=15_000
        )
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
        )
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        handler.addFilter(TelemetryRedactionFilter())
        logging.getLogger().addHandler(handler)
        _configured = True
        logger.info("telemetry configured for %s -> %s", service_name, endpoint)
    except Exception:  # pragma: no cover - defensive; export must never break startup
        logger.exception("telemetry configuration failed; continuing without export")
