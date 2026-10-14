"""Queued exports cannot become active across a Core maintenance barrier."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from inspect import unwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.feedback.export_execution import prepare_export
from coire_core.models.auth import UserRole
from coire_core.settings import Settings


async def test_queued_export_keeps_no_active_claim_during_core_maintenance() -> None:
    actor = Principal(
        kind=PrincipalKind.ADMIN, user_id=uuid4(), role=UserRole.ADMIN, scopes=frozenset({"admin"})
    )
    export = SimpleNamespace(
        state="queued",
        authorization_snapshot=actor.model_dump(mode="json"),
        queue_deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = export
    session.scalar.return_value = "coire-scheduler"

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    with (
        patch("coire_api.feedback.export_execution.session_scope", sessions),
        patch("coire_api.feedback.export_execution.authorize_admin", new_callable=AsyncMock),
        patch(
            "coire_api.feedback.export_execution.select_export_sources", new_callable=AsyncMock
        ) as select_sources,
    ):
        assert await prepare_export("export", Settings(upgrades_enabled=True)) == "queued"
        select_sources.assert_not_called()
        session.commit.assert_not_called()
        assert export.state == "queued"
        assert "upgrade_maintenance" in str(session.scalar.call_args.args[0])
        assert any("190034190034" in str(call.args[0]) for call in session.execute.call_args_list)


async def test_queued_file_work_cannot_cross_file_worker_maintenance() -> None:
    from coire_scheduler.files import _prepare

    job = SimpleNamespace(
        state="queued",
        attachment_id=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        operation="inspect",
        selected_pages=[],
        principal_subject="user",
        source_key=str(uuid4()),
    )
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = job
    session.scalar.return_value = "coire-file-worker"

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    with patch("coire_scheduler.files.session_scope", sessions):
        assert await _prepare("file-job", Settings(upgrades_enabled=True)) is None
        assert job.state == "queued"
        assert "upgrade_maintenance" in str(session.scalar.call_args.args[0])


async def test_fenced_file_workflow_waits_and_resumes_same_job() -> None:
    from coire_scheduler.files import file_processing_workflow

    step = AsyncMock(side_effect=[False, False, True])
    sleep = AsyncMock()
    with (
        patch("coire_scheduler.files.file_processing_step", step),
        patch("coire_scheduler.files.DBOS.sleep_async", sleep),
    ):
        # Execute the workflow body independently of the DBOS deployment; the
        # production decorators still own persistence and step replay.
        await unwrap(file_processing_workflow)("retained-job")
    assert [call.args for call in step.await_args_list] == [("retained-job",)] * 3
    assert [call.args for call in sleep.await_args_list] == [(1,), (1,)]


async def test_queued_acquisition_keeps_its_stage_unclaimed_when_fenced() -> None:
    from coire_core.models.acquisition import AcquisitionStage, AcquisitionState
    from coire_scheduler.acquisition import _start_stage

    workflow = SimpleNamespace(state=AcquisitionState.QUEUED, variant_id=uuid4(), attempt=1)
    session = AsyncMock(spec=AsyncSession)
    session.get.side_effect = [workflow, SimpleNamespace()]
    result = Mock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    session.scalar.return_value = "core"

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    with (
        patch("coire_scheduler.acquisition.session_scope", sessions),
        patch(
            "coire_scheduler.acquisition.get_settings", return_value=Settings(upgrades_enabled=True)
        ),
    ):
        assert await _start_stage(uuid4(), AcquisitionStage.PULL) is False
    assert workflow.state is AcquisitionState.QUEUED
    session.add.assert_not_called()


async def test_queued_image_dispatch_is_not_claimed_under_core_fence() -> None:
    from coire_scheduler.image_dispatch import prepare_image_dispatch

    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = "core"
    row = SimpleNamespace(
        state="queued",
        cancel_requested_at=None,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session.get.return_value = row
    with (
        patch("coire_scheduler.image_dispatch._placed", return_value=False),
        patch(
            "coire_scheduler.image_dispatch._policy",
            side_effect=AssertionError("fenced claim reached policy"),
        ),
    ):
        assert (
            await prepare_image_dispatch(session, "image", Settings(upgrades_enabled=True)) is None
        )
    assert row.state == "queued"
    assert "190034190034" in str(session.execute.call_args_list[0].args[0])


async def test_queued_training_remains_queued_without_preflight_under_core_fence() -> None:
    from coire_scheduler.training import advance_training

    principal = Principal(kind=PrincipalKind.ADMIN, user_id=uuid4(), role=UserRole.ADMIN)
    job = SimpleNamespace(
        id="training",
        state="queued",
        authorization_snapshot=principal.model_dump(mode="json"),
        queue_deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        execution_deadline_at=datetime.now(UTC) + timedelta(minutes=10),
        version=1,
    )
    session = AsyncMock(spec=AsyncSession)

    async def scalar(query: object) -> str | None:
        return "core" if "upgrade_maintenance" in str(query) else None

    session.scalar.side_effect = scalar
    with (
        patch("coire_scheduler.training.current_job", AsyncMock(return_value=job)),
        patch("coire_scheduler.training.authorize_live_training_action", AsyncMock()),
        patch("coire_core.settings.get_settings", return_value=Settings(upgrades_enabled=True)),
        patch("coire_api.training.events.append_event", AsyncMock()) as event,
    ):
        await advance_training(session, job.id)
    assert job.state == "queued" and job.version == 1
    event.assert_not_called()


async def test_direct_training_admission_checks_core_before_actor_or_job_locks() -> None:
    from coire_scheduler.training_admission import admit_training

    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = "core"
    with (
        patch("coire_core.settings.get_settings", return_value=Settings(upgrades_enabled=True)),
        patch(
            "coire_scheduler.training_admission.authorize_live_training_action", AsyncMock()
        ) as auth,
    ):
        assert await admit_training(session, SimpleNamespace(), []) is None
    auth.assert_not_called()
    session.get.assert_not_called()


async def test_queued_evaluation_does_not_claim_a_phase_under_core_fence() -> None:
    from coire_scheduler.evaluations import advance

    principal = Principal(kind=PrincipalKind.ADMIN, user_id=uuid4(), role=UserRole.ADMIN)
    run = SimpleNamespace(
        id="evaluation",
        state="queued",
        started_at=None,
        started_deadline_at=None,
        queue_deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        authorization_snapshot=principal.model_dump(mode="json"),
    )
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = "core"
    rows = Mock()
    rows.all.return_value = []
    session.scalars.return_value = rows
    with (
        patch("coire_scheduler.evaluations.current_run", AsyncMock(return_value=run)),
        patch("coire_scheduler.evaluations.authorize_live_evaluation_action", AsyncMock()),
        patch("coire_api.evaluation.checkpoints.require_checkpoint_run_owner", AsyncMock()),
    ):
        assert not await advance(session, run.id, Settings(upgrades_enabled=True))
    assert run.state == "queued"
    session.add.assert_not_called()


async def test_queued_agent_does_not_transition_to_placing_under_core_fence() -> None:
    from coire_core.models.runs import AgentRunState
    from coire_scheduler.runs import _advance

    row = SimpleNamespace(state=AgentRunState.QUEUED)
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = row
    session.scalar.return_value = "core"

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    with (
        patch("coire_scheduler.runs.session_scope", sessions),
        patch("coire_scheduler.runs.get_settings", return_value=Settings(upgrades_enabled=True)),
        patch("coire_scheduler.runs.transition", AsyncMock()) as transition,
    ):
        assert not await _advance(uuid4(), AgentRunState.PLACING, "placement")
    transition.assert_not_called()
    assert row.state is AgentRunState.QUEUED


async def test_conversion_stage_maintenance_wait_does_not_exhaust_failure_retries() -> None:
    from coire_core.errors import UpgradeUnavailable
    from coire_scheduler.acquisition import run_stage

    with (
        patch(
            "coire_scheduler.acquisition._context",
            AsyncMock(return_value={"inspection": {}, "model_slug": "model"}),
        ),
        patch("coire_scheduler.acquisition._start_stage", AsyncMock(return_value=True)),
        patch(
            "coire_scheduler.acquisition._run_command_stage",
            AsyncMock(side_effect=UpgradeUnavailable("Maintenance retains ownership")),
        ),
    ):
        assert not await unwrap(run_stage)(str(uuid4()), "convert")
