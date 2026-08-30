"""OpenTelemetry wiring (FR-014).

Everything exports to the local collector on core and stays there — no telemetry egresses
(research R11). Exporter failures are logged, never raised: a collector outage must not turn
into a request failure (contracts/compose-topology.md — nothing depends on the collector).
"""

from __future__ import annotations

import logging

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)
_configured = False


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
        _configured = True
        logger.info("telemetry configured for %s -> %s", service_name, endpoint)
    except Exception:  # pragma: no cover - defensive; export must never break startup
        logger.exception("telemetry configuration failed; continuing without export")
