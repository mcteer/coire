"""Bounded local telemetry; disabled diagnostics create no trace exporter."""

import resource
import sys
from collections.abc import Iterable

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

from coire_core.settings import Settings


def configure(settings: Settings) -> tuple[MeterProvider, TracerProvider]:
    identity = Resource.create(
        {"service.name": "coire-failover", "service.version": settings.service_version}
    )
    tracing = TracerProvider(
        resource=identity, sampler=None if settings.diagnostics_enabled else ALWAYS_OFF
    )
    if settings.diagnostics_enabled:
        tracing.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True, timeout=1),
                max_queue_size=256,
                max_export_batch_size=64,
                export_timeout_millis=1000,
            )
        )
    metering = MeterProvider(
        resource=identity,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=settings.otlp_endpoint, insecure=True, timeout=1),
                export_interval_millis=15000,
                export_timeout_millis=1000,
            )
        ],
    )
    trace.set_tracer_provider(tracing)
    metrics.set_meter_provider(metering)

    def memory(_options: CallbackOptions) -> Iterable[Observation]:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        yield Observation(rss if sys.platform == "darwin" else rss * 1024)

    metering.get_meter("coire.failover").create_observable_gauge(
        "coire_failover_frontend_rss_bytes",
        callbacks=[memory],
        unit="By",
        description="Peak resident frontend memory, bounded by its container limit",
    )
    return metering, tracing
