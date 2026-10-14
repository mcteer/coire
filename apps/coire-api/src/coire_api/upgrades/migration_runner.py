"""Fixed one-shot image entrypoint: ordinary install or one consumed managed attempt."""

import asyncio
import signal
import sys
from datetime import UTC, datetime
from typing import Any

from alembic import command
from alembic.config import Config

from coire_api.db import dispose_engine, init_engine, session_scope
from coire_api.upgrades.migration import claim_migration, record_migration
from coire_core.errors import UpgradeForbidden
from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeMigrationExecution
from coire_core.settings import Settings


async def _claim(settings: Settings) -> tuple[UpgradeExecutionPlan, UpgradeMigrationExecution]:
    if settings.upgrade_migration_job_id is None or settings.upgrade_migration_generation is None:
        raise UpgradeForbidden("Managed migration requires its fixed operation identity")
    init_engine(settings)
    try:
        async with asyncio.timeout(5), session_scope() as session:
            return await claim_migration(
                session,
                settings.upgrade_migration_job_id,
                settings.upgrade_migration_generation,
                settings,
            )
    finally:
        await dispose_engine()


async def _record(
    settings: Settings,
    plan: UpgradeExecutionPlan,
    intent: UpgradeMigrationExecution,
    exit_code: int,
) -> UpgradeMigrationExecution:
    init_engine(settings)
    try:
        async with asyncio.timeout(5), session_scope() as session:
            return await record_migration(session, plan, intent, exit_code=exit_code)
    finally:
        await dispose_engine()


def _deadline(_signal: int, _frame: Any) -> None:
    raise TimeoutError("captured migration deadline elapsed")


def main() -> int:
    if len(sys.argv) != 1:
        return 2
    settings = Settings()
    managed = settings.upgrade_migration_job_id is not None
    if not managed and settings.upgrade_migration_generation is not None:
        return 2
    config = Config("/app/alembic.ini")
    if not managed:
        command.upgrade(config, "head")
        return 0
    try:
        plan, intent = asyncio.run(_claim(settings))
    except Exception:
        print('{"state":"refused","reason":"migration_authority_unavailable"}', flush=True)
        return 1
    if intent.state == "complete":
        print(intent.model_dump_json(), flush=True)
        return 0
    remaining = (intent.deadline_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return 1  # Retain intent; an expired attempt never receives a new budget.
    config.attributes["coire_upgrade_deadline"] = intent.deadline_at
    previous = signal.signal(signal.SIGALRM, _deadline)
    exit_code = 1
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining)
        command.upgrade(config, intent.expected_schema_head)
        exit_code = 0
    except Exception:
        # The retained attempt remains uncertain until bounded schema observation.
        # Never serialize database exceptions or credentials to container logs.
        pass
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    try:
        result = asyncio.run(_record(settings, plan, intent, exit_code))
    except Exception:
        print('{"state":"uncertain","reason":"migration_schema_unavailable"}', flush=True)
        return 1
    print(result.model_dump_json(), flush=True)
    return 0 if result.state == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
