"""Database engine and session management.

`pool_pre_ping` and a short connect timeout are what let a restarting Postgres produce a fast,
honest `unhealthy` and then recover without restarting the API (spec US2 scenario 2).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from coire_core.models.node import NodeRole, Reachability
from coire_core.settings import Settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    pass


class NodeRow(Base):
    """The only persisted entity in feature 000 (data-model.md)."""

    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[NodeRole] = mapped_column(
        SAEnum(NodeRole, name="node_role", values_callable=lambda e: [m.value for m in e])
    )
    mesh_address: Mapped[str] = mapped_column(INET)
    egress_address: Mapped[str] = mapped_column(INET)
    # 256 GB of RAM and 1.8 TB of disk both overflow a 32-bit column.
    memory_total_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger)
    gpu_cores: Mapped[int | None] = mapped_column(nullable=True)
    agent_version: Mapped[str] = mapped_column(String(32))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reachability: Mapped[Reachability] = mapped_column(
        SAEnum(Reachability, name="reachability", values_callable=lambda e: [m.value for m in e]),
        default=Reachability.UNKNOWN,
    )
    probe_failures: Mapped[int] = mapped_column(default=0)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine. Kept separate from `init_engine` so tests can make their own."""
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args={"timeout": 5.0, "command_timeout": 10.0},
    )


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_engine(settings)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("engine not initialised; call init_engine() during startup")
    async with _sessionmaker() as session:
        yield session
