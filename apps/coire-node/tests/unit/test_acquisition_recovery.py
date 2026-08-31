from __future__ import annotations

import uuid
from datetime import UTC, datetime

from coire_core.models.jobs import JobKind, JobStage, JobStatus
from coire_core.settings import Settings
from coire_node.jobs import JobSupervisor
from coire_node.store import Store


def test_resume_restarts_only_incomplete_jobs(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(  # type: ignore[call-arg]
        node_state_dir=str(tmp_path / "state"),
        node_store_dir=str(tmp_path / "models"),
        node_hf_cache_dir=str(tmp_path / "cache"),
        _secrets_dir="/nonexistent",
    )
    supervisor = JobSupervisor(settings, Store(settings.node_store_dir))
    now = datetime.now(UTC)
    active = JobStatus(
        job_id=uuid.uuid4(),
        kind=JobKind.CONVERT,
        slug="model.active",
        stage=JobStage.TRANSFERRING,
        started_at=now,
        updated_at=now,
    )
    done = JobStatus(
        job_id=uuid.uuid4(),
        kind=JobKind.PULL,
        slug="model.done",
        stage=JobStage.DONE,
        started_at=now,
        updated_at=now,
    )
    supervisor._write({}, active)
    supervisor._write({}, done)
    spawned: list[uuid.UUID] = []
    monkeypatch.setattr(supervisor, "_spawn", spawned.append)

    assert supervisor.resume_all() == 1
    assert spawned == [active.job_id]


def test_duplicate_job_id_attaches_without_spawning_twice(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(  # type: ignore[call-arg]
        node_state_dir=str(tmp_path / "state"),
        node_store_dir=str(tmp_path / "models"),
        node_hf_cache_dir=str(tmp_path / "cache"),
        _secrets_dir="/nonexistent",
    )
    store = Store(settings.node_store_dir)
    supervisor = JobSupervisor(settings, store)
    spawned: list[uuid.UUID] = []
    monkeypatch.setattr(supervisor, "_spawn", spawned.append)
    job_id = uuid.uuid4()

    created, first = supervisor.start(
        job_id=job_id, kind=JobKind.CLEANUP, slug="model.raw", params={}
    )
    attached, second = supervisor.start(
        job_id=job_id, kind=JobKind.CLEANUP, slug="model.raw", params={}
    )

    assert created is True
    assert attached is False
    assert first.job_id == second.job_id
    assert spawned == [job_id]
