"""Core GET streams retain admission until completion while maintenance stays observable."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.upgrades.admission import CoreAdmissionMiddleware, CoreAdmissions


async def test_core_stream_drains_and_upgrade_progress_remains_reachable() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = None

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    started, finish = asyncio.Event(), asyncio.Event()
    app = FastAPI()
    admissions = CoreAdmissions(enabled=True)
    app.add_middleware(CoreAdmissionMiddleware, admissions=admissions)

    @app.get("/api/v1/runs/example/events")
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            started.set()
            yield b"before"
            await finish.wait()
            yield b"after"

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.get("/api/v1/admin/upgrades/jobs/example/events")
    async def progress() -> dict[str, bool]:
        return {"observable": True}

    with patch("coire_api.upgrades.admission.session_scope", sessions):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://core"
        ) as client:
            pending = asyncio.create_task(client.get("/api/v1/runs/example/events"))
            await asyncio.wait_for(started.wait(), timeout=2)
            session.scalar.return_value = "coire-api"
            try:
                assert (await admissions.inventory(("coire-api",))).entries
                assert not (await admissions.inventory(("coire-file-worker",))).entries
                refused = await client.get("/api/v1/runs/example/events")
                assert refused.status_code == 503 and refused.headers["retry-after"] == "5"
                assert (
                    await client.get("/api/v1/admin/upgrades/jobs/example/events")
                ).status_code == 200
            finally:
                finish.set()
                response = await asyncio.wait_for(pending, timeout=2)
            assert response.content == b"beforeafter"
            assert not (await admissions.inventory(("coire-api",))).entries


async def test_measurement_does_not_inspect_inputs_or_take_holds_during_core_drain() -> None:
    from unittest.mock import Mock

    from coire_core.settings import Settings
    from coire_scheduler.training_measurements import admit_measurement

    session = AsyncMock(spec=AsyncSession)
    row, binding = Mock(), Mock()
    with patch("coire_scheduler.maintenance.allows_core_work", AsyncMock(return_value=False)):
        assert (
            await admit_measurement(session, row, binding, settings=Settings(upgrades_enabled=True))
            is None
        )
    session.scalars.assert_not_awaited()
    session.add.assert_not_called()


async def test_queued_evaluation_measurement_waits_before_claim_locks() -> None:
    import inspect
    from types import SimpleNamespace

    from coire_core.settings import Settings
    from coire_scheduler.evaluation_measurements import claim_measurement

    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = SimpleNamespace(state="pending")

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    with (
        patch("coire_scheduler.evaluation_measurements.session_scope", sessions),
        patch(
            "coire_scheduler.evaluation_measurements.get_settings",
            return_value=Settings(upgrades_enabled=True),
        ),
        patch("coire_scheduler.maintenance.allows_core_work", AsyncMock(return_value=False)),
    ):
        assert not await inspect.unwrap(claim_measurement)("00000000-0000-0000-0000-000000000001")
    session.execute.assert_not_awaited()


async def test_queued_dataset_analysis_waits_without_claiming_node_memory() -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from coire_core.settings import Settings
    from coire_scheduler.datasets import DatasetAnalysisExecutor

    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = SimpleNamespace(state="queued", result=None)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    with (
        patch("coire_scheduler.datasets.session_scope", sessions),
        patch("coire_scheduler.maintenance.allows_core_work", AsyncMock(return_value=False)),
    ):
        await DatasetAnalysisExecutor(Settings(upgrades_enabled=True))._advance(uuid4())
    assert session.get.await_count == 1
    session.scalars.assert_not_awaited()
    session.add.assert_not_called()


async def test_run_placement_rechecks_maintenance_after_health_probe() -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from coire_core.models.runs import AgentRunState
    from coire_core.settings import Settings
    from coire_scheduler.runs import commit_run_placement

    session = AsyncMock(spec=AsyncSession)
    run = SimpleNamespace(node_id=None, state=AgentRunState.PLACING)
    session.get.return_value = run

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    with (
        patch("coire_scheduler.runs.session_scope", sessions),
        patch("coire_scheduler.runs.get_settings", return_value=Settings(upgrades_enabled=True)),
        patch("coire_scheduler.maintenance.allows_core_work", AsyncMock(return_value=True)),
        patch("coire_scheduler.maintenance.allows_admission", AsyncMock(return_value=False)),
        patch("coire_api.placement.service.lock_nodes_for_admission", AsyncMock()) as locked,
    ):
        node_id = uuid4()
        assert not await commit_run_placement(uuid4(), node_id)
        locked.assert_awaited_once_with(session, [node_id])
    assert run.node_id is None


async def test_comparison_waits_for_core_fence_without_losing_queued_execution() -> None:
    from coire_api.feedback.comparison_execution import admitted_comparison_session
    from coire_core.settings import Settings

    session = AsyncMock(spec=AsyncSession)
    opened = 0

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        nonlocal opened
        opened += 1
        yield session

    allowed = AsyncMock(side_effect=[False, False, True])
    with (
        patch("coire_api.feedback.comparison_execution.session_scope", sessions),
        patch("coire_scheduler.maintenance.allows_core_work", allowed),
        patch("coire_api.feedback.comparison_execution.asyncio.sleep", AsyncMock()) as pause,
    ):
        async with admitted_comparison_session(Settings(upgrades_enabled=True)) as admitted:
            assert admitted is session
            assert opened == 3
            assert pause.await_count == 2
    session.add.assert_not_called()


async def test_legacy_engine_hold_cannot_be_created_under_node_maintenance() -> None:
    from uuid import uuid4

    import pytest

    from coire_api.placement.legacy import ensure_legacy_model_hold
    from coire_core.errors import UpgradeUnavailable

    session = AsyncMock(spec=AsyncSession)
    with (
        patch("coire_scheduler.maintenance.allows_admission", AsyncMock(return_value=False)),
        pytest.raises(UpgradeUnavailable),
    ):
        await ensure_legacy_model_hold(session, uuid4(), uuid4(), 1024)
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


async def test_queued_model_placement_waits_before_creating_ledger_holds() -> None:
    from uuid import uuid4

    from coire_core.settings import Settings
    from coire_scheduler.placement import _run_decision

    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = None

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    with (
        patch("coire_scheduler.placement.session_scope", sessions),
        patch(
            "coire_scheduler.placement.get_settings", return_value=Settings(upgrades_enabled=True)
        ),
        patch("coire_scheduler.maintenance.allows_core_work", AsyncMock(return_value=False)),
        patch("coire_scheduler.placement.ensure_ledgers", AsyncMock()) as ledgers,
    ):
        assert not await _run_decision(uuid4())
    ledgers.assert_not_awaited()
    session.add.assert_not_called()


async def test_evaluation_phase_waits_before_new_sandbox_admission() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from coire_core.settings import Settings
    from coire_scheduler.evaluation_admission import reserve_phase

    session = AsyncMock(spec=AsyncSession)
    with patch("coire_scheduler.maintenance.allows_core_work", AsyncMock(return_value=False)):
        assert not await reserve_phase(
            session,
            Mock(),
            SimpleNamespace(sandbox_reservation_id=None),
            Mock(),
            Settings(upgrades_enabled=True),
        )
    session.execute.assert_not_awaited()
    session.add.assert_not_called()


async def test_placement_workflow_keeps_accepted_decision_while_fenced() -> None:
    import inspect

    from coire_scheduler.placement import placement_workflow

    decision = "00000000-0000-0000-0000-000000000001"
    with (
        patch(
            "coire_scheduler.placement.execute_placement",
            AsyncMock(side_effect=[False, False, True]),
        ) as execute,
        patch("coire_scheduler.placement.DBOS.sleep_async", AsyncMock()) as pause,
    ):
        await inspect.unwrap(placement_workflow)(decision)
    assert execute.await_count == 3
    assert all(call.args == (decision,) for call in execute.await_args_list)
    assert pause.await_count == 2
