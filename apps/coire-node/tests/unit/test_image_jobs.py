"""Durable image job identity and restart behavior without a native worker."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from coire_core.models.image_worker import NodeImageStartRequest
from coire_core.models.images import ImageSpec, ResolvedImageSpec, canonical_spec_hash
from coire_node.image_cleanup import ImageCleanupUnavailable, discard_cancelled_image_scratch
from coire_node.image_jobs import ImageJobJournal, ImageJournalConflict, ImageJournalUnavailable

JOB = "01J00000000000000000000000"
NODE = "coire-edge-b"
MODEL = uuid.UUID("10000000-0000-0000-0000-000000000001")
INSTANCE = uuid.UUID("20000000-0000-0000-0000-000000000001")


def _request() -> NodeImageStartRequest:
    spec = ImageSpec(
        model_id=MODEL,
        prompt="fox",
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("3.25"),
        seed=7,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )
    return NodeImageStartRequest(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        model_id=MODEL,
        instance_id=INSTANCE,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
        reservation_bytes=1024,
    )


def test_journal_replay_restart_and_fence_conflict(tmp_path: Path) -> None:
    journal = ImageJobJournal(tmp_path, NODE)
    request = _request()
    queued = journal.begin(request)
    assert queued.state == "queued"
    path = tmp_path / "image-jobs" / f"{JOB}.json"
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0

    restarted = ImageJobJournal(tmp_path, NODE)
    assert restarted.begin(request) == queued
    running = queued.model_copy(update={"state": "reserving", "updated_at": datetime.now(UTC)})
    assert restarted.advance(running) == running
    assert journal.get(JOB) == running
    assert journal.request(JOB) == request
    with pytest.raises(ImageJournalConflict):
        restarted.begin(request.model_copy(update={"fence": 5}))
    with pytest.raises(ImageJournalConflict):
        restarted.begin(request.model_copy(update={"attempt": 2}))


def test_journal_terminal_and_uncertain_state_never_restarts(tmp_path: Path) -> None:
    journal = ImageJobJournal(tmp_path, NODE)
    request = _request()
    queued = journal.begin(request)
    cancelled = queued.model_copy(update={"state": "cancelled", "updated_at": datetime.now(UTC)})
    journal.advance(cancelled)
    assert journal.begin(request) == cancelled
    discard_cancelled_image_scratch(journal, tmp_path, cancelled)
    cleaned = journal.advance(
        cancelled.model_copy(
            update={
                "scratch_cleaned": True,
                "updated_at": cancelled.updated_at + timedelta(microseconds=1),
            }
        )
    )
    assert cleaned.scratch_cleaned is True
    with pytest.raises(ImageJournalConflict):
        journal.advance(
            cancelled.model_copy(update={"state": "running", "updated_at": datetime.now(UTC)})
        )

    path = tmp_path / "image-jobs" / f"{JOB}.json"
    path.write_text("broken")
    with pytest.raises(ImageJournalUnavailable):
        journal.begin(request)
    with pytest.raises(ImageJournalUnavailable):
        journal.get(JOB)
    assert path.read_text() == "broken"


def test_cancelled_scratch_refuses_symlink_and_retries_after_removal(tmp_path: Path) -> None:
    journal = ImageJobJournal(tmp_path, NODE)
    queued = journal.begin(_request())
    root = tmp_path / "image-scratch"
    attempt = root / f"{JOB}-1-4"
    attempt.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    (attempt / "0.png").symlink_to(outside)
    with pytest.raises(ImageCleanupUnavailable):
        discard_cancelled_image_scratch(journal, tmp_path, queued)
    assert outside.read_bytes() == b"keep"
    (attempt / "0.png").unlink()
    output = attempt / "0.png"
    output.write_bytes(b"private output")
    output.chmod(0o600)
    discard_cancelled_image_scratch(journal, tmp_path, queued)
    assert not attempt.exists()
    assert outside.read_bytes() == b"keep"


def test_journal_refuses_unsafe_path_or_public_file(tmp_path: Path) -> None:
    journal = ImageJobJournal(tmp_path, NODE)
    with pytest.raises(ImageJournalConflict):
        journal.get("../escape")
    request = _request()
    journal.begin(request)
    path = tmp_path / "image-jobs" / f"{JOB}.json"
    os.chmod(path, 0o644)
    with pytest.raises(ImageJournalUnavailable):
        journal.get(JOB)


def test_journal_refuses_hardlinked_record_without_mutating_it(tmp_path: Path) -> None:
    journal = ImageJobJournal(tmp_path, NODE)
    request = _request()
    journal.begin(request)
    path = tmp_path / "image-jobs" / f"{JOB}.json"
    alias = tmp_path / "journal-alias"
    os.link(path, alias)
    original = alias.read_bytes()
    restarted = ImageJobJournal(tmp_path, NODE)
    for operation in (
        lambda: restarted.get(JOB),
        lambda: restarted.request(JOB),
        lambda: restarted.begin(request),
    ):
        with pytest.raises(ImageJournalUnavailable):
            operation()
    assert path.read_bytes() == alias.read_bytes() == original


def test_journal_rejects_wrong_node_and_expired_new_request(tmp_path: Path) -> None:
    journal = ImageJobJournal(tmp_path, NODE)
    request = _request()
    with pytest.raises(ImageJournalConflict):
        journal.begin(request.model_copy(update={"node": "coire-edge-a"}))
    with pytest.raises(ImageJournalConflict):
        journal.begin(
            request.model_copy(update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)})
        )
    assert journal.get(JOB) is None
