"""Human-owned idempotent submission and versioned control, independent of scores."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from opentelemetry import trace
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    EvaluationEvidenceRow,
    EvaluationGroupRow,
    EvaluationResultRow,
    EvaluationRunRow,
    EvaluationSuiteRow,
)
from coire_api.evaluation.authorization import (
    authorize_live_evaluation_action,
    reject_self_judge,
    resolve_evaluation_target,
)
from coire_api.evaluation.catalog import get_row, project
from coire_api.evaluation.events import append, current_run
from coire_api.evaluation.evidence import reserve_quota
from coire_api.evaluation.execution import stop_children
from coire_api.nodes_client import NodeClient
from coire_api.training.service import training_id
from coire_core.errors import (
    EvaluationConflict,
    EvaluationDisabled,
    EvaluationQuotaExceeded,
    EvaluationValidationError,
)
from coire_core.models.evaluation import (
    TERMINAL_EVALUATION_STATES,
    EvaluationControl,
    EvaluationEvidence,
    EvaluationReceipt,
    EvaluationResult,
    EvaluationRunDetail,
    EvaluationState,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationTarget,
    SuiteKind,
    SuiteMode,
)
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.api.evaluation")


def validate_subjects(suite: EvaluationSuite, subjects: list[EvaluationTarget]) -> None:
    if (
        not subjects
        or len(subjects) > 2
        or (suite.template.kind == SuiteKind.HARNESS and len(subjects) != 1)
        or (suite.template.mode == SuiteMode.PAIRWISE and len(subjects) != 2)
    ):
        raise EvaluationValidationError("Subject count differs from suite mode")
    if len({subject.target for subject in subjects}) != len(subjects):
        raise EvaluationValidationError("Evaluation subjects must be distinct")
    reject_self_judge(
        [subject.target for subject in subjects], suite.judge.target if suite.judge else None
    )


def receipt(run: EvaluationRunRow) -> EvaluationReceipt:
    return EvaluationReceipt(
        id=run.id,
        group_id=run.group_id,
        state=EvaluationState(run.state),
        version=run.version,
        events_path=f"/api/v1/admin/evaluations/{run.id}/events",
    )


async def detail(session: AsyncSession, run_id: str) -> EvaluationRunDetail:
    run = await current_run(session, run_id, lock=False)
    result_row = await session.scalar(
        select(EvaluationResultRow).where(EvaluationResultRow.run_id == run.id)
    )
    evidence_rows = (
        await session.scalars(
            select(EvaluationEvidenceRow)
            .where(EvaluationEvidenceRow.run_id == run.id)
            .order_by(EvaluationEvidenceRow.created_at)
        )
    ).all()
    group = await session.get(EvaluationGroupRow, run.group_id)
    assert group is not None
    suite = EvaluationSuite.model_validate(run.suite_snapshot)
    catalog = await session.get(EvaluationSuiteRow, run.suite_row_id, populate_existing=True)
    if catalog is not None:
        # Presentation follows current retirement while result provenance keeps
        # the original immutable definition and content digest.
        suite = suite.model_copy(
            update={"retired": catalog.retired, "registry_version": catalog.registry_version}
        )
    return EvaluationRunDetail.model_validate(
        {
            **receipt(run).model_dump(),
            "owner_user_id": run.owner_user_id,
            "suite": suite,
            "subjects": [EvaluationTarget.model_validate(value) for value in run.subjects],
            "phase": run.phase,
            "cleanup_state": run.cleanup_state,
            "reason": run.safe_failure_code,
            "queue_deadline": run.queue_deadline_at,
            "execution_deadline": run.execution_deadline_at,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "source_run_id": run.source_run_id,
            "training_job_id": group.job_id,
            "checkpoint_id": group.checkpoint_id,
            "update": group.completed_update,
            "result": EvaluationResult.model_validate(result_row.result) if result_row else None,
            "evidence": [
                EvaluationEvidence.model_validate(
                    {
                        "id": row.id,
                        "sha256": row.sha256,
                        "bytes": row.bytes,
                        "expires_at": row.expires_at,
                        "availability": "expired"
                        if row.expires_at <= datetime.now(UTC)
                        else row.availability,
                    }
                )
                for row in evidence_rows
            ],
        }
    )


async def submit(
    session: AsyncSession,
    principal: Principal,
    request: EvaluationSubmission,
    *,
    idempotency_key: str,
    settings: Settings,
    client: NodeClient,
    source_run_id: str | None = None,
    origin: Literal["manual", "measurement"] = "manual",
) -> EvaluationReceipt:
    with tracer.start_as_current_span(
        "coire.api.evaluation.submit", record_exception=False, set_status_on_exception=False
    ):
        owner = await authorize_live_evaluation_action(session, principal)
        if not 1 <= len(idempotency_key) <= 128 or any(
            ord(char) < 33 or ord(char) > 126 for char in idempotency_key
        ):
            raise EvaluationValidationError("Idempotency key must be bounded printable ASCII")
        operation = f"rerun:{source_run_id}" if source_run_id else "submit"
        key_digest = hashlib.sha256(f"{operation}:{idempotency_key}".encode()).hexdigest()
        request_digest = hashlib.sha256(
            json.dumps(
                {"submission": request.model_dump(mode="json"), "source_run_id": source_run_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        existing = await session.scalar(
            select(EvaluationRunRow).where(
                EvaluationRunRow.owner_user_id == owner,
                EvaluationRunRow.idempotency_key_sha256 == key_digest,
            )
        )
        if existing is not None:
            if existing.request_sha256 != request_digest:
                raise EvaluationConflict("Idempotency key already binds a different evaluation")
            return receipt(existing)
        if not settings.evaluations_enabled:
            raise EvaluationDisabled()
        pending = await session.scalar(
            select(func.count())
            .select_from(EvaluationRunRow)
            .where(
                EvaluationRunRow.state.notin_([state.value for state in TERMINAL_EVALUATION_STATES])
            )
        )
        if int(pending or 0) >= settings.evaluation_max_pending_runs:
            raise EvaluationQuotaExceeded("Evaluation queue is full")
        catalog = await get_row(session, request.suite_id, request.suite_version, admit=True)
        suite = project(catalog)
        targets = [
            await resolve_evaluation_target(
                session, principal, subject, client, checkpoint_measurement=origin == "measurement"
            )
            for subject in request.subjects
        ]
        if request.expected_engine_version is not None and any(
            target.runtime.engine_version != request.expected_engine_version for target in targets
        ):
            raise EvaluationConflict("Expected engine version differs from Studio runtime")
        validate_subjects(suite, targets)
        data_snapshot = None
        checkpoint_id = None
        completed_update = None
        if request.training_job_id is not None:
            from coire_api.evaluation.inputs import manual_training_context

            data_snapshot, checkpoint = await manual_training_context(
                session,
                request.training_job_id,
                targets,
                checkpoint_measurement=origin == "measurement",
            )
            checkpoint_id, completed_update = checkpoint.id, checkpoint.completed_update
        if source_run_id is not None:
            await current_run(session, source_run_id, lock=False)
        # Owner and any training job locks precede global queue/quota locks.
        await session.execute(text("SELECT pg_advisory_xact_lock(170017)"))
        pending = await session.scalar(
            select(func.count())
            .select_from(EvaluationRunRow)
            .where(
                EvaluationRunRow.state.notin_([state.value for state in TERMINAL_EVALUATION_STATES])
            )
        )
        if int(pending or 0) >= settings.evaluation_max_pending_runs:
            raise EvaluationQuotaExceeded("Evaluation queue is full")
        now = datetime.now(UTC)
        group_id, run_id = training_id(), training_id()
        session.add(
            EvaluationGroupRow(
                id=group_id,
                owner_user_id=owner,
                origin=origin,
                job_id=request.training_job_id,
                checkpoint_id=checkpoint_id,
                completed_update=completed_update,
                subjects=[target.model_dump(mode="json") for target in targets],
                created_at=now,
            )
        )
        await session.flush()
        run = EvaluationRunRow(
            id=run_id,
            group_id=group_id,
            owner_user_id=owner,
            suite_row_id=catalog.id,
            suite_snapshot=suite.model_dump(mode="json"),
            subjects=[target.model_dump(mode="json") for target in targets],
            source_run_id=source_run_id,
            data_snapshot=data_snapshot,
            authorization_snapshot=principal.model_dump(mode="json"),
            request_sha256=request_digest,
            idempotency_key_sha256=key_digest,
            state="queued",
            phase=None,
            version=1,
            fence=1,
            cleanup_state="complete",
            evidence_reserved_bytes=0,
            next_event_sequence=1,
            queue_deadline_at=now + timedelta(seconds=settings.evaluation_queue_timeout_seconds),
            execution_deadline_at=now
            + timedelta(seconds=settings.evaluation_queue_timeout_seconds + suite.timeout_seconds),
            created_at=now,
        )
        session.add(run)
        await session.flush()
        await reserve_quota(session, run, settings)
        await append(session, run)
        await write_principal_audit(
            session,
            principal=principal,
            action="evaluation.rerun" if source_run_id else "evaluation.submit",
            target_type="evaluation_run",
            target_id=run.id,
            context={
                "group_id": group_id,
                "suite_sha256": suite.content_sha256,
                "source_run_id": source_run_id,
            },
        )
        return receipt(run)


async def cancel(
    session: AsyncSession, principal: Principal, run_id: str, request: EvaluationControl
) -> EvaluationReceipt:
    await authorize_live_evaluation_action(session, principal)
    run = await current_run(session, run_id)
    if run.version != request.expected_version:
        raise EvaluationConflict("Evaluation control version is stale")
    if EvaluationState(run.state) not in TERMINAL_EVALUATION_STATES and run.state != "cancelling":
        run.state, run.safe_failure_code = "cancelling", "cancelled"
        await stop_children(session, run.id)
        run.version += 1
        await append(session, run)
        await write_principal_audit(
            session,
            principal=principal,
            action="evaluation.cancel",
            target_type="evaluation_run",
            target_id=run.id,
        )
    return receipt(run)
