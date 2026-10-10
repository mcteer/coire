"""Bounded request pool whose first readonly query also checks connection liveness."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import metrics, trace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.training")
reconnects = metrics.get_meter("coire.api.training").create_counter(
    "coire_training_measurement_database_reconnects_total"
)


class MeasurementDatabase:
    """Use the configured database and credentials without a redundant ping query.

    Only the internal generation route uses this pool. Its initial readonly
    lookup may reconnect once when SQLAlchemy invalidates a broken connection.
    Admission writes, commits and upstream generation are never replayed.
    """

    def __init__(self, settings: Settings) -> None:
        self.engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=False,
            pool_recycle=300,
            pool_size=2,
            max_overflow=2,
            connect_args={
                "timeout": 5.0,
                "command_timeout": 10.0,
                "server_settings": {"application_name": "coire-api.training-measurements"},
            },
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session
            await session.commit()

    async def close(self) -> None:
        await self.engine.dispose()


def record_reconnect(measurement_id: uuid.UUID) -> None:
    with tracer.start_as_current_span("coire.api.training.measurement.database_reconnect"):
        reconnects.add(1)
        logger.warning(
            "retrying invalidated measurement readonly database lookup",
            extra={"measurement_id": str(measurement_id)},
        )
