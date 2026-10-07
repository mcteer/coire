"""A fresh worker process must activate instruments created before SDK startup."""

import json
import os
import subprocess
import sys


def test_worker_sdk_exports_existing_span_and_completed_update_counter() -> None:
    source = """
import json, os
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from coire_node import otel
from coire_node.training import worker
from coire_node.training.telemetry import initialize_training_telemetry
spans = InMemorySpanExporter()
reader = InMemoryMetricReader()
otel.OTLPSpanExporter = lambda **kwargs: spans
otel.OTLPMetricExporter = lambda **kwargs: object()
otel.PeriodicExportingMetricReader = lambda *args, **kwargs: reader
os.environ['OTLP_ENDPOINT'] = 'http://coire-core.lab:4317'
initialize_training_telemetry()
with worker.tracer.start_as_current_span('coire.node.training.execute'):
    worker.updates.add(1, {'node':'coire-edge-a'})
trace.get_tracer_provider().force_flush()
data = reader.get_metrics_data()
metrics = [(m.name, sum(p.value for p in m.data.data_points))
           for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics]
print(json.dumps({'spans':[s.name for s in spans.get_finished_spans()], 'metrics':metrics}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={**os.environ, "OTEL_SDK_DISABLED": "false"},
    )
    result = json.loads(completed.stdout)
    assert result["spans"] == ["coire.node.training.execute"]
    assert ["coire_training_completed_updates_total", 1] in result["metrics"]


def test_worker_entrypoint_attributes_loading_and_execution_to_same_owned_trace() -> None:
    source = """
import json, os, uuid
from pathlib import Path
from types import SimpleNamespace
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from coire_node import otel
from coire_node.training import worker
spans = InMemorySpanExporter()
otel.OTLPSpanExporter = lambda **kwargs: spans
otel.OTLPMetricExporter = lambda **kwargs: object()
otel.PeriodicExportingMetricReader = lambda *args, **kwargs: InMemoryMetricReader()
os.environ['OTLP_ENDPOINT'] = 'http://coire-core.lab:4317'
worker.platform.node = lambda: 'coire-edge-a'
worker.platform.system = lambda: 'Darwin'
worker.platform.machine = lambda: 'arm64'
args = SimpleNamespace(attempt='01M4A6FSQ3Q2TWXGA0MH011AQ0', owner=uuid.uuid4(),
    state_root=Path('/synthetic/state'), store_root=Path('/synthetic/models'),
    artifact_root=Path('/synthetic/artifacts'))
worker.argparse.ArgumentParser.parse_args = lambda self: args
worker.read_private = lambda *args: b'{}'
prepared = SimpleNamespace(attempt_id=args.attempt, job_id='01M4A6FSASWNDAD6MNMJ35YMC7', node='coire-edge-a')
worker.TrainingPrepareRequest.model_validate_json = lambda body: prepared
worker.TrainingJournal = lambda *args, **kwargs: SimpleNamespace(close=lambda: None)
def execute(*args, **kwargs):
    with worker.tracer.start_as_current_span('coire.node.training.load'):
        pass
    with worker.tracer.start_as_current_span('coire.node.training.execute'):
        pass
    return 0
worker.execute_native = execute
assert worker.main() == 0
trace.get_tracer_provider().force_flush()
finished = spans.get_finished_spans()
root = next((s for s in finished if s.name == 'coire.node.training.worker'), None)
print(json.dumps({'owned_root': root is not None,
    'job_id': root.attributes.get('job_id') if root else None,
    'same_parent': bool(root) and all(s.parent is not None and
        s.parent.span_id == root.context.span_id and s.context.trace_id == root.context.trace_id
        for s in finished if s != root)}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={**os.environ, "OTEL_SDK_DISABLED": "false"},
    )
    result = json.loads(completed.stdout)
    assert result == {
        "owned_root": True,
        "job_id": "01M4A6FSASWNDAD6MNMJ35YMC7",
        "same_parent": True,
    }
