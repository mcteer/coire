"""CPU-only executor export and bounded replay diagnostics, independent of diagnostics mode."""

import logging
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from coire_core.settings import Settings

logger = logging.getLogger(__name__)
meter = metrics.get_meter("coire.upgrade_executor")
replays = meter.create_counter("coire_upgrade_executor_receipt_replay_total", unit="")
replay_duration = meter.create_histogram(
    "coire_upgrade_executor_receipt_replay_duration_seconds", unit="s"
)
source_configuration = meter.create_counter(
    "coire_upgrade_source_configuration_observations_total", unit=""
)
_configured = False


def bootstrap_is_fresh(verified: bool, observed_at: datetime | None) -> bool:
    return bool(
        verified
        and observed_at is not None
        and observed_at.utcoffset() is not None
        and 0 <= (datetime.now(UTC) - observed_at).total_seconds() <= 10
    )


def configure(settings: Settings) -> None:
    """Export failure cannot grant authority or interrupt restoration."""
    global _configured
    if _configured:
        return
    resource = Resource.create(
        {"service.name": "coire-upgrade-executor", "service.version": settings.service_version}
    )
    try:
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)
        metrics.set_meter_provider(
            MeterProvider(
                resource=resource,
                metric_readers=[
                    PeriodicExportingMetricReader(
                        OTLPMetricExporter(endpoint=settings.otlp_endpoint, insecure=True),
                        export_interval_millis=5000,
                    )
                ],
            )
        )
        _configured = True
    except Exception:
        logger.warning("executor telemetry export initialization unavailable")
