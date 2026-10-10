"""Bounded request traces preserve causality and suppress private exceptions."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from coire_api.training import telemetry


@pytest.mark.parametrize("linked_root", [False, True])
async def test_observed_request_preserves_parent_or_links_new_root(
    monkeypatch: pytest.MonkeyPatch, linked_root: bool
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    monkeypatch.setattr(telemetry, "tracer", tracer)

    @telemetry.observed("request", linked_root=linked_root)
    async def request() -> int:
        return trace.get_current_span().get_span_context().trace_id

    with tracer.start_as_current_span("qualification") as parent:
        origin = parent.get_span_context()
        request_trace = await request()
        assert trace.get_current_span().get_span_context() == origin
    spans = {span.name: span for span in exporter.get_finished_spans()}
    child = spans["request"]
    if linked_root:
        assert request_trace != origin.trace_id
        assert child.parent is None
        assert len(child.links) == 1 and child.links[0].context == origin
    else:
        assert request_trace == origin.trace_id
        assert child.parent == origin and not child.links
    provider.shutdown()


async def test_linked_root_without_parent_keeps_private_failure_out_of_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "tracer", provider.get_tracer(__name__))

    @telemetry.observed("request", linked_root=True)
    async def request() -> None:
        raise ValueError("private prompt")

    with pytest.raises(ValueError, match="private prompt"):
        await request()
    span = exporter.get_finished_spans()[0]
    assert span.parent is None and not span.links and not span.events
    assert "private prompt" not in str(span.attributes)
    provider.shutdown()
