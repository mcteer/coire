"""Private bounded evidence bytes; PostgreSQL owns quota and expiry authority."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import EvaluationEvidenceRow, EvaluationRunRow
from coire_api.evaluation.authorization import authorize_live_evaluation_action
from coire_core.errors import (
    EvaluationConflict,
    EvaluationEvidenceGone,
    EvaluationNotFound,
    EvaluationQuotaExceeded,
    EvaluationValidationError,
)
from coire_core.models.evaluation import MAX_EVALUATION_BYTES, EvaluationWorkerResult
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.api.evaluation")
expired = metrics.get_meter("coire.api.evaluation").create_counter(
    "coire_evaluation_evidence_expired_total"
)


class EvidenceStore:
    def __init__(self, settings: Settings) -> None:
        parent = Path(settings.training_dataset_dir)
        if not parent.is_absolute() or parent.is_symlink():
            raise EvaluationValidationError("Evidence volume must be a private directory")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.stat().st_mode & 0o077:
            raise EvaluationValidationError("Evidence volume permissions must be private")
        root = parent / "evaluation-evidence"
        if root.is_symlink():
            raise EvaluationValidationError("Evidence namespace must not be linked")
        root.mkdir(mode=0o700, exist_ok=True)
        if root.stat().st_mode & 0o077:
            raise EvaluationValidationError("Evidence namespace permissions must be private")
        self.root = root.resolve()

    def _path(self, identity: uuid.UUID) -> Path:
        if not isinstance(identity, uuid.UUID):
            raise EvaluationValidationError("Evidence requires a generated identity")
        path = self.root / str(identity)
        if path.is_symlink():
            raise EvaluationValidationError("Evidence path must not be linked")
        return path

    def _read(self, identity: uuid.UUID, digest: str) -> bytes:
        path = self._path(identity)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > MAX_EVALUATION_BYTES
                ):
                    raise EvaluationValidationError("Evidence file is unsafe")
                data = source.read(MAX_EVALUATION_BYTES + 1)
                if len(data) != info.st_size or hashlib.sha256(data).hexdigest() != digest:
                    raise EvaluationValidationError("Evidence content differs from receipt")
                return data
        except FileNotFoundError as error:
            raise EvaluationEvidenceGone() from error

    async def read(self, identity: uuid.UUID, digest: str) -> bytes:
        return await asyncio.to_thread(self._read, identity, digest)

    def _sync(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stage(self, identity: uuid.UUID, data: bytes, digest: str) -> int:
        if len(data) > MAX_EVALUATION_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise EvaluationValidationError("Evidence bytes or digest are invalid")
        path = self._path(identity)
        if path.exists():
            try:
                if self._read(identity, digest) == data:
                    return len(data)
            except EvaluationValidationError as error:
                raise EvaluationConflict(
                    "Evidence identity already contains different bytes"
                ) from error
        temporary = self.root / f"stage-{identity}-{uuid.uuid4()}"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                if self._read(identity, digest) != data:
                    raise EvaluationConflict("Evidence identity conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        self._sync()
        return len(data)

    async def stage(self, identity: uuid.UUID, data: bytes, digest: str) -> int:
        with tracer.start_as_current_span(
            "coire.api.evaluation.evidence.stage",
            record_exception=False,
            set_status_on_exception=False,
        ):
            return await asyncio.to_thread(self._stage, identity, data, digest)

    async def remove(self, identity: uuid.UUID) -> None:
        def remove() -> None:
            self._path(identity).unlink(missing_ok=True)
            # Stop/collection serialization is enforced by the parent row lock.
            # Recover only temporary bytes carrying this generated receipt ID.
            for temporary in self.root.glob(f"stage-{identity}-*"):
                suffix = temporary.name.removeprefix(f"stage-{identity}-")
                try:
                    if str(uuid.UUID(suffix)) != suffix:
                        continue
                except ValueError:
                    continue
                temporary.unlink(missing_ok=True)
            self._sync()

        await asyncio.to_thread(remove)


async def reserve_quota(session: AsyncSession, run: EvaluationRunRow, settings: Settings) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(170027)"))
    if run.evidence_reserved_bytes:
        return
    held = await session.scalar(
        select(func.coalesce(func.sum(EvaluationRunRow.evidence_reserved_bytes), 0))
    )
    if int(held or 0) + MAX_EVALUATION_BYTES > settings.evaluation_evidence_quota_bytes:
        raise EvaluationQuotaExceeded("Evaluation evidence quota is full")
    run.evidence_reserved_bytes = MAX_EVALUATION_BYTES


async def lookup(
    session: AsyncSession, principal: Principal, identity: uuid.UUID, store: EvidenceStore
) -> bytes:
    await authorize_live_evaluation_action(session, principal)
    row = await session.get(EvaluationEvidenceRow, identity)
    if row is None:
        raise EvaluationNotFound()
    if row.availability != "present" or row.expires_at <= datetime.now(UTC):
        raise EvaluationEvidenceGone()
    if row.storage_key != str(row.id):
        raise EvaluationValidationError("Evidence storage receipt is invalid")
    return await store.read(row.id, row.sha256)


async def expire(session: AsyncSession, store: EvidenceStore, *, now: datetime) -> int:
    rows = (
        await session.scalars(
            select(EvaluationEvidenceRow)
            .where(
                EvaluationEvidenceRow.availability == "present",
                EvaluationEvidenceRow.expires_at <= now,
            )
            .with_for_update(skip_locked=True)
            .limit(100)
        )
    ).all()
    count = 0
    for row in rows:
        if row.pin_until is not None and row.pin_until > now:
            continue
        await store.remove(row.id)
        row.availability = "expired"
        count += 1
    await session.flush()
    if rows:
        for run_id in sorted({row.run_id for row in rows}):
            run = await session.get(EvaluationRunRow, run_id, with_for_update=True)
            retained = await session.scalar(
                select(EvaluationEvidenceRow.id)
                .where(
                    EvaluationEvidenceRow.run_id == run_id,
                    EvaluationEvidenceRow.availability == "present",
                )
                .limit(1)
            )
            if (
                run is not None
                and run.state in {"succeeded", "failed", "timed_out", "cancelled"}
                and run.cleanup_state == "complete"
                and retained is None
            ):
                await session.execute(text("SELECT pg_advisory_xact_lock(170027)"))
                run.evidence_reserved_bytes = 0
    expired.add(count)
    return count


async def persist_collected(
    session: AsyncSession, attempt_id: uuid.UUID, result: EvaluationWorkerResult, settings: Settings
) -> uuid.UUID:
    from datetime import timedelta

    from coire_api.db import EvaluationAttemptRow
    from coire_api.evaluation.validation import validate_worker_result
    from coire_core.models.evaluation import EvaluationWorkload

    attempt = await session.get(EvaluationAttemptRow, attempt_id, populate_existing=True)
    if attempt is None:
        raise EvaluationNotFound()
    run = await session.get(
        EvaluationRunRow, attempt.run_id, populate_existing=True, with_for_update=True
    )
    attempt = await session.get(
        EvaluationAttemptRow, attempt_id, populate_existing=True, with_for_update=True
    )
    assert attempt is not None
    if (
        run is None
        or run.fence != attempt.fence
        or result.fence != attempt.fence
        or result.attempt_id != attempt.id
    ):
        raise EvaluationConflict("Collected evidence fence is stale")
    workload = EvaluationWorkload.model_validate(attempt.workload)
    validate_worker_result(workload, result)
    payload = result.model_dump_json().encode()
    digest = hashlib.sha256(payload).hexdigest()
    identity = uuid.uuid5(attempt.id, "evaluation-evidence-v1")
    existing = await session.get(EvaluationEvidenceRow, identity)
    if existing is not None:
        if existing.sha256 != digest or existing.attempt_id != attempt.id:
            raise EvaluationConflict("Collected evidence identity conflict")
        if existing.availability != "present" or existing.expires_at <= datetime.now(UTC):
            raise EvaluationEvidenceGone()
        await EvidenceStore(settings).read(identity, digest)
        return identity
    if run.state not in {"preparing", "reserving", "running", "collecting"}:
        raise EvaluationConflict("Evaluation no longer accepts new worker evidence")
    if run.evidence_reserved_bytes != MAX_EVALUATION_BYTES:
        raise EvaluationQuotaExceeded("Collected evidence has no quota reservation")
    retained = await session.scalar(
        select(func.coalesce(func.sum(EvaluationEvidenceRow.bytes), 0)).where(
            EvaluationEvidenceRow.run_id == run.id
        )
    )
    if int(retained or 0) + len(payload) > MAX_EVALUATION_BYTES:
        raise EvaluationQuotaExceeded("Evaluation evidence exceeds its per-run bound")
    store = EvidenceStore(settings)
    await store.stage(identity, payload, digest)
    now = datetime.now(UTC)
    session.add(
        EvaluationEvidenceRow(
            id=identity,
            run_id=run.id,
            attempt_id=attempt.id,
            storage_key=str(identity),
            sha256=digest,
            bytes=len(payload),
            availability="present",
            created_at=now,
            expires_at=now + timedelta(days=settings.evaluation_evidence_retention_days),
            pin_until=run.execution_deadline_at,
        )
    )
    attempt.collected_sha256 = digest
    attempt.state = "collected"
    await session.flush()
    from coire_api.evaluation.events import append

    completed = {(item.case_id, item.subject_index) for item in result.outputs}
    completed.update((item.case_id, item.subject_index) for item in result.scores)
    completed.update((item.case_id, -1) for item in result.pairwise)
    await append(session, run, kind="progress", completed_cases=len(completed))
    return identity
