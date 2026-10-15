"""Core transitions survive DB failure/restart and compact only after committed audit rows."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_control_plane_failover import _members, _settle

from coire_api.db import AuditRow, Base, FailoverEventReceiptRow
from coire_api.failover import poller as module
from coire_api.failover.participant import CoreParticipant, core_participant
from coire_core.failover_journal import ElectionJournal
from coire_core.models.failover import FailoverEventKind
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


async def test_core_journal_retains_uncommitted_events_and_reconciles_exactly_once(
    tmp_path: Path,
    training_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = "failover_core_" + uuid4().hex
    owner = create_async_engine(training_postgres_url)
    async with owner.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    private, participants, membership = _members()
    journal = ElectionJournal(tmp_path / "coire-core.events.json")
    core = CoreParticipant(
        name="coire-core",
        membership=membership,
        private_key_b64=private["coire-core"],
        timings=participants["coire-core"].timings,
        journal=journal,
    )
    participants["coire-core"] = core
    _settle(participants, datetime.now(UTC))
    assert core.frontend_ready()
    core._fence(outage=True)
    entries = journal.entries()
    assert {item.kind for item in entries} == {FailoverEventKind.PROMOTED, FailoverEventKind.FENCED}
    fail_commit = True

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with sessions.begin() as session:
            yield session
            if fail_commit:
                raise OSError("isolated failure before durable commit")

    monkeypatch.setattr(module, "session_scope", scope)
    poller = module.CoreFailoverPoller(core, Settings(upgrades_enabled=False))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await poller._flush_events()
        assert ElectionJournal(tmp_path / "coire-core.events.json").entries() == entries
        async with sessions() as session:
            assert not (await session.scalars(select(FailoverEventReceiptRow))).all()
        fail_commit = False
        await poller._flush_events()
        assert not journal.entries()
        # Crash after DB commit but before local acknowledgement is a duplicate retry.
        for event in entries:
            journal.append(event)
        await poller._flush_events()
        assert not journal.entries()
        async with sessions() as session:
            assert len((await session.scalars(select(FailoverEventReceiptRow))).all()) == 2
            assert len((await session.scalars(select(AuditRow))).all()) == 2
    finally:
        await poller.stop()
        await engine.dispose()
        async with owner.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner.dispose()


def test_production_core_factory_attaches_persistent_journal(tmp_path: Path) -> None:
    from pydantic import SecretStr

    private, _, membership = _members()
    participant = core_participant(
        Settings(
            failover_peer_key=SecretStr(private["coire-core"]),
            failover_snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        membership,
    )
    assert participant is not None
    assert isinstance(participant._journal, ElectionJournal)
    assert participant._journal._path == tmp_path / "coire-core.events.json"
