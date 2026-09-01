"""Best-effort local OTLP export for the native node agent."""

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
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("opentelemetry.exporter"):
            return False
        for key in tuple(record.__dict__):
            if any(fragment in key.lower() for fragment in SENSITIVE_FIELD_FRAGMENTS):
                record.__dict__[key] = "[REDACTED]"
        return True


def configure_node_telemetry(version: str, endpoint: str) -> None:
    resource = Resource.create(
        {"service.name": "coire-node", "service.version": version, "network.path": "control"}
    )
    try:
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        trace.set_tracer_provider(tracer_provider)
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
    except Exception:
        logger.exception("node telemetry configuration failed; continuing")
