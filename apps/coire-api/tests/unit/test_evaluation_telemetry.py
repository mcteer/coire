"""Tracing failures cannot serialize private raw exception values."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

WORKLOAD_FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


async def test_observed_evaluation_failure_suppresses_exception_recording_and_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from coire_api.evaluation import telemetry

    options: dict[str, Any] = {}

    @contextmanager
    def span(name: str, **kwargs: Any) -> Iterator[None]:
        options.update(kwargs)
        yield None

    monkeypatch.setattr(telemetry, "tracer", Mock(start_as_current_span=span))

    @telemetry.observed("coire.scheduler.evaluation.phase")
    async def fail() -> None:
        raise ValueError("private candidate output must stay private")

    with (
        caplog.at_level("INFO", logger="coire_api.evaluation.telemetry"),
        pytest.raises(ValueError),
    ):
        await fail()
    assert options["record_exception"] is False
    assert options["set_status_on_exception"] is False
    assert "private candidate" not in caplog.text
    assert caplog.records[0].__dict__["operation"] == "coire.scheduler.evaluation.phase"


async def test_private_evidence_validation_error_has_safe_durable_step_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    import coire_scheduler.evaluations as module
    from coire_api.db import EvaluationAttemptRow, EvaluationEvidenceRow, EvaluationRunRow
    from coire_core.errors import EvaluationConflict
    from coire_core.models.evaluation import EvaluationWorkload
    from coire_core.settings import Settings

    workload = EvaluationWorkload.model_validate_json(WORKLOAD_FIXTURE.read_bytes())
    attempt = EvaluationAttemptRow(
        id=workload.attempt_id,
        run_id=workload.evaluation_id,
        workload=workload.model_dump(mode="json"),
        ordinal=1,
        collected_sha256="a" * 64,
    )
    session = AsyncMock()
    session.scalars.return_value.all = lambda: [attempt]
    session.get.return_value = EvaluationEvidenceRow(
        id=uuid.uuid4(),
        availability="present",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        sha256="a" * 64,
    )
    store = AsyncMock()
    store.read.return_value = b'{"private_output":"sensitive candidate text"}'
    monkeypatch.setattr(module, "EvidenceStore", lambda settings: store)
    with pytest.raises(EvaluationConflict) as failure:
        await module.load_evidence(session, EvaluationRunRow(id=workload.evaluation_id), Settings())
    assert "sensitive candidate" not in str(failure.value)
    assert failure.value.__suppress_context__


async def test_evaluation_command_failure_does_not_log_private_node_detail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import uuid
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    import coire_api.run_executor as module
    from coire_api.db import AgentRunRow, RunCommandRow
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_core.models.runs import RunOperation
    from coire_core.settings import Settings

    command = RunCommandRow(id=uuid.uuid4(), run_id=uuid.uuid4(), operation=RunOperation.COLLECT)
    child = AgentRunRow(id=command.run_id, purpose="evaluation")
    session = AsyncMock()
    session.get.side_effect = [command, child]

    @asynccontextmanager
    async def scope() -> Any:
        yield session

    monkeypatch.setattr(module, "session_scope", scope)
    executor = module.RunCommandExecutor(Settings())
    failure = NodeError(
        NodeErrorKind.PROTOCOL,
        "coire-edge-a",
        detail="private candidate output",
        body={"detail": {"code": "private candidate output"}},
    )
    with caplog.at_level("ERROR", logger="coire_api.run_executor"):
        await executor._failed(command.id, failure)
    assert "private candidate" not in caplog.text
    assert "private candidate" not in str(command.detail)
