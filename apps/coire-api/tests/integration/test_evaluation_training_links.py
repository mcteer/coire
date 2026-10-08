"""Training keeps its success while durable evaluation links progress independently."""

import pytest
from evaluation_training_fixtures import seed_evaluated_training
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import Base, TrainingAdapterRow, TrainingJobRow
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


async def test_training_adapter_and_activity_links_include_pending_obligation_and_failed_group(
    training_postgres_url: str,
) -> None:
    from coire_api.auth import Principal
    from coire_api.console.training import project_training_activity
    from coire_api.evaluation.training import ensure_final_trigger, reconcile_final_trigger
    from coire_api.training.adapters import adapter_evaluation_detail
    from coire_api.training.service import job_detail

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, adapter_id = await seed_evaluated_training(session)
            job = await session.get(TrainingJobRow, job_id, with_for_update=True)
            adapter = await session.get(TrainingAdapterRow, adapter_id)
            assert job is not None and adapter is not None
            trigger = await ensure_final_trigger(
                session, job, adapter, settings=Settings(evaluations_enabled=False)
            )
            assert trigger is not None
            detail = await job_detail(session, job_id)
            assert len(detail.evaluation_groups) == 1
            link = detail.evaluation_groups[0]
            assert (
                link.trigger_id == trigger.id and link.group_id is None and link.state == "pending"
            )
            view = await adapter_evaluation_detail(session, adapter)
            assert (
                view.evaluation_groups == detail.evaluation_groups
                and view.evaluation_id == adapter.evaluation_id
            )
            actor = Principal.model_validate(job.authorization_snapshot)
            activity = await project_training_activity(session, actor)
            assert activity.items[0].evaluation_groups == detail.evaluation_groups
            assert activity.items[0].total_updates == 32
            await reconcile_final_trigger(
                session,
                trigger.id,
                settings=Settings(evaluations_enabled=False, evaluation_queue_timeout_seconds=60),
            )
            # The pending trigger's original durable queue deadline is bounded independently.
            from datetime import UTC, datetime, timedelta

            trigger.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            await reconcile_final_trigger(
                session, trigger.id, settings=Settings(evaluations_enabled=False)
            )
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            detail = await job_detail(recovered, job_id)
            link = detail.evaluation_groups[0]
            assert link.group_id is not None and link.state == "failed"
            assert len(link.run_ids) == len(link.result_ids) == 2
            assert detail.state == "succeeded"
    finally:
        await engine.dispose()
