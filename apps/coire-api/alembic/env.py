"""Alembic environment (async).

Runs in the one-shot `coire-migrate` container, never inside a long-lived service (FR-010).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection

from coire_api.db import Base
from coire_core.settings import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    deadline = config.attributes.get("coire_upgrade_deadline")
    if deadline is not None:
        if not isinstance(deadline, datetime) or deadline.tzinfo is None:
            raise RuntimeError("managed migration deadline is invalid")
        remaining_ms = int((deadline - datetime.now(UTC)).total_seconds() * 1000)
        if remaining_ms <= 0 or remaining_ms > 120_000:
            raise RuntimeError("managed migration deadline elapsed or differs")
        connection.execute(
            text("SELECT set_config('statement_timeout', :timeout, false)"),
            {"timeout": str(remaining_ms)},
        )
        connection.commit()
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
