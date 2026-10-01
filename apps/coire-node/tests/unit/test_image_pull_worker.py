"""Image pull must use the pinned, filtered path and refuse incomplete trees."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_core.models.jobs import JobKind, JobStage, JobStatus, RepoFile, RepoInspection
from coire_node import hub
from coire_node.worker import EXIT_FAILED, EXIT_OK, JobFile, run_pull


def _job(tmp_path: Path) -> JobFile:
    path = tmp_path / "job.json"
    now = datetime.now(UTC)
    status = JobStatus(
        job_id=uuid.uuid4(),
        kind=JobKind.PULL,
        slug="org--image",
        stage=JobStage.QUEUED,
        started_at=now,
        updated_at=now,
    )
    path.write_text(
        json.dumps(
            {
                "status": status.model_dump(mode="json"),
                "params": {
                    "store_dir": str(tmp_path / "store"),
                    "repo_id": "org/image",
                    "revision": "a" * 40,
                    "model_kind": "image_model",
                },
            }
        )
    )
    return JobFile(path)


def _inspection() -> RepoInspection:
    digest = hashlib.sha256(b"data").hexdigest()
    return RepoInspection(
        repo_id="org/image",
        revision="a" * 40,
        files=[
            RepoFile(path="config.json", bytes=2),
            RepoFile(path="model.safetensors", bytes=4, upstream_sha256=digest),
            RepoFile(path="unsafe.pkl", bytes=3),
        ],
        total_bytes=9,
        weight_bytes=4,
        is_mlx_format=False,
        license_id="apache-2.0",
    )


def test_image_pull_fails_when_unselected_file_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub, "inspect", lambda *args, **kwargs: _inspection())

    def download(repo: RepoInspection, kind: object, *, local_dir: Path, token: str | None) -> str:
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text("{}")
        (local_dir / "model.safetensors").write_bytes(b"data")
        (local_dir / "unsafe.pkl").write_bytes(b"bad")
        return str(local_dir)

    monkeypatch.setattr(hub, "snapshot_image_asset", download)
    job = _job(tmp_path)
    assert run_pull(job) == EXIT_FAILED
    assert job.status.stage == "failed"


def test_image_pull_uses_filtered_snapshot_and_rejects_revision_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inspected = _inspection()
    inspected.revision = "c" * 40
    monkeypatch.setattr(hub, "inspect", lambda *args, **kwargs: inspected)
    monkeypatch.setattr(
        hub,
        "snapshot",
        lambda *args, **kwargs: pytest.fail("generic snapshot must not run for image assets"),
    )
    monkeypatch.setattr(
        hub,
        "snapshot_image_asset",
        lambda *args, **kwargs: pytest.fail("revision drift must prevent image download"),
    )
    job = _job(tmp_path)
    assert run_pull(job) == EXIT_FAILED
    assert job.status.stage == "failed"


def test_image_pull_hashes_only_selected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub, "inspect", lambda *args, **kwargs: _inspection())
    monkeypatch.setattr(
        hub,
        "snapshot",
        lambda *args, **kwargs: pytest.fail("generic snapshot must not run for image assets"),
    )

    def download(repo: RepoInspection, kind: object, *, local_dir: Path, token: str | None) -> str:
        assert repo.revision == "a" * 40
        assert kind == "image_model"
        local_dir.mkdir(parents=True)
        (local_dir / "config.json").write_text("{}")
        (local_dir / "model.safetensors").write_bytes(b"data")
        return str(local_dir)

    monkeypatch.setattr(hub, "snapshot_image_asset", download)
    job = _job(tmp_path)
    assert run_pull(job) == EXIT_OK
    assert job.status.stage == "done"
    assert job.status.manifest is not None
    assert {item.path for item in job.status.manifest.files} == {
        "config.json",
        "model.safetensors",
    }
