"""Low-cardinality harness telemetry with no prompt or output content."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import metrics, trace

tracer = trace.get_tracer("coire.agent.harness")
meter = metrics.get_meter("coire.agent.harness")
runs = meter.create_counter("coire_harness_runs", unit="{run}")
failures = meter.create_counter("coire_harness_failures", unit="{failure}")
retries = meter.create_histogram("coire_harness_retries", unit="{retry}")
truncations = meter.create_counter("coire_harness_context_truncations", unit="{message}")


@contextmanager
def harness_span(profile: str, strategy: str) -> Iterator[trace.Span]:
    attributes = {"coire.profile": profile, "coire.strategy": strategy}
    with tracer.start_as_current_span("coire.agent.run", attributes=attributes) as span:
        yield span
