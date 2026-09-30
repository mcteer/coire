"""Authenticated, assigned-run activity spool reads fail closed."""

from __future__ import annotations

import io
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from coire_core.models.runs import RunActivity, RunActivityPage, RunActivityTool
from coire_core.settings import Settings
from coire_node import runs as runs_module
from coire_node.runs import RunManager, RunRuntimeError
from coire_node.testing.harness import TOKEN, Agent


def _archive(
    run_id: uuid.UUID, records: list[RunActivity], *, filename: str | None = None
) -> bytes:
    payload = b"".join(record.model_dump_json().encode() + b"\n" for record in records)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(filename or f"activity-{run_id}.jsonl")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def _record(run_id: uuid.UUID, sequence: int, *, overflow: bool = False) -> RunActivity:
    return RunActivity(
        run_id=run_id,
        sequence=sequence,
        tool_name=RunActivityTool.ACTIVITY_SPOOL if overflow else RunActivityTool.READ_FILE,
        state="failed" if overflow else "completed",
        created_at=datetime.now(UTC),
        safe_error="limit_reached" if overflow else None,
    )


class Docker:
    def __init__(self, run_id: uuid.UUID, archive: bytes | None) -> None:
        self.run_id = run_id
        self.archive_bytes = archive
        self.archived_path: str | None = None
        self.labels: dict[str, str] = {
            "com.coire.agent-run": str(run_id),
            "com.coire.managed": "true",
            "com.coire.node": "edge-a",
            "com.coire.result-path": "/coire-output/result.json",
        }

    async def inspect_container(self, _name: str) -> dict[str, Any]:
        return {"Config": {"Labels": self.labels}}

    async def archive(self, _name: str, path: str) -> bytes | None:
        self.archived_path = path
        return self.archive_bytes


def _manager(run_id: uuid.UUID, archive: bytes | None) -> tuple[RunManager, Docker]:
    docker = Docker(run_id, archive)
    settings = Settings(_secrets_dir="/nonexistent", node_name="edge-a")  # type: ignore[call-arg]
    return RunManager(settings, docker), docker  # type: ignore[arg-type]


async def test_activity_reader_pages_assigned_run_and_reports_overflow() -> None:
    run_id = uuid.uuid4()
    records = [_record(run_id, sequence) for sequence in range(1, 106)]
    records.append(_record(run_id, 106, overflow=True))
    manager, docker = _manager(run_id, _archive(run_id, records))
    first = await manager.activity(run_id)
    assert len(first.data) == 100
    assert first.next_sequence == 100
    assert first.truncated and first.available
    second = await manager.activity(run_id, after_sequence=first.next_sequence)
    assert [item.sequence for item in second.data] == [101, 102, 103, 104, 105, 106]
    assert second.next_sequence is None
    assert docker.archived_path == f"/coire-output/activity-{run_id}.jsonl"


async def test_activity_span_only_carries_run_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(runs_module, "_tracer", provider.get_tracer("test.node.activity"))
    run_id = uuid.uuid4()
    manager, _ = _manager(run_id, _archive(run_id, [_record(run_id, 1)]))
    assert (await manager.activity(run_id)).data[0].sequence == 1
    span = exporter.get_finished_spans()[0]
    assert span.name == "coire.node.run.activity"
    assert span.attributes == {"run_id": str(run_id)}
    assert span.events == ()


async def test_activity_reader_unavailable_without_separate_output_or_spool() -> None:
    run_id = uuid.uuid4()
    manager, docker = _manager(run_id, None)
    assert not (await manager.activity(run_id)).available
    docker.labels["com.coire.result-path"] = "/workspace/.coire/result.json"
    assert not (await manager.activity(run_id)).available


@pytest.mark.parametrize(
    "failure",
    ["foreign", "duplicate", "name", "oversize", "marker", "unknown_tool", "extra", "malformed"],
)
async def test_activity_reader_refuses_invalid_archive(failure: str) -> None:
    run_id = uuid.uuid4()
    records = [_record(run_id, 1), _record(run_id, 2)]
    filename = None
    if failure == "foreign":
        records[1] = _record(uuid.uuid4(), 2)
    elif failure == "duplicate":
        records[1] = _record(run_id, 1)
    elif failure == "name":
        filename = "other.jsonl"
    elif failure == "marker":
        records[0] = _record(run_id, 1, overflow=True)
    elif failure == "unknown_tool":
        records[1].tool_name = cast(RunActivityTool, "private_filepath")
    archive = _archive(run_id, records, filename=filename)
    if failure == "oversize":
        archive += b"x" * (4 * 1024 * 1024)
    elif failure == "extra":
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as bundle:
            first = tarfile.TarInfo(f"activity-{run_id}.jsonl")
            first.size = len(archive)
            bundle.addfile(first, io.BytesIO(archive))
            extra = tarfile.TarInfo("unexpected")
            extra.size = 0
            bundle.addfile(extra, io.BytesIO())
        archive = output.getvalue()
    elif failure == "malformed":
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as bundle:
            bad = b"{not-json}\n"
            member = tarfile.TarInfo(f"activity-{run_id}.jsonl")
            member.size = len(bad)
            bundle.addfile(member, io.BytesIO(bad))
        archive = output.getvalue()
    manager, _ = _manager(run_id, archive)
    with pytest.raises(RunRuntimeError) as exc:
        await manager.activity(run_id)
    assert exc.value.code == "run_activity_unreadable"


async def test_activity_reader_refuses_unassigned_container_before_archive() -> None:
    run_id = uuid.uuid4()
    manager, docker = _manager(run_id, _archive(run_id, [_record(run_id, 1)]))
    docker.labels["com.coire.agent-run"] = str(uuid.uuid4())
    with pytest.raises(RunRuntimeError) as exc:
        await manager.activity(run_id)
    assert exc.value.code == "run_container_missing"
    assert docker.archived_path is None


def test_activity_route_requires_node_auth_and_returns_typed_page(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    agent = Agent(tmp_path)
    app = agent.app()

    class StubRuns:
        async def activity(
            self, requested: uuid.UUID, *, after_sequence: int, limit: int
        ) -> RunActivityPage:
            assert requested == run_id and after_sequence == 4 and limit == 100
            return RunActivityPage(run_id=run_id, data=[_record(run_id, 5)])

    app.state.runs = StubRuns()
    path = f"/node/runs/{run_id}/activity?after_sequence=4"
    try:
        with TestClient(app) as anonymous:
            assert anonymous.get(path).status_code == 401
        with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            response = client.get(path)
            assert response.status_code == 200
            assert response.json()["data"][0]["sequence"] == 5
            assert (
                client.get(f"/node/runs/{run_id}/activity?after_sequence=10001").status_code == 422
            )
    finally:
        agent.close()
