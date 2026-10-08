"""Immutable registered versions of installed project-authored suites."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import EvaluationSuiteRow
from coire_api.evaluation.authorization import authorize_live_evaluation_action
from coire_core.errors import EvaluationConflict, EvaluationNotFound
from coire_core.evaluation_suites import template
from coire_core.models.evaluation import (
    EvaluationSuite,
    EvaluationSuiteRegistration,
    EvaluationTarget,
)

tracer = trace.get_tracer("coire.api.evaluation")


def definition_digest(suite: EvaluationSuite) -> str:
    return hashlib.sha256(
        json.dumps(
            suite.model_dump(
                mode="json",
                include={
                    "suite_id",
                    "version",
                    "template",
                    "generation",
                    "timeout_seconds",
                    "judge",
                    "judge_generation",
                },
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def build_suite(
    request: EvaluationSuiteRegistration,
    *,
    owner: uuid.UUID | None,
    judge: EvaluationTarget | None,
    now: datetime,
) -> EvaluationSuite:
    if request.judge is not None and (
        judge is None
        or (judge.target.model_id, judge.target.variant_id, judge.target.adapter_id)
        != (request.judge.model_id, request.judge.variant_id, request.judge.adapter_id)
    ):
        raise EvaluationConflict("Judge identity differs from registration")
    suite = EvaluationSuite(
        suite_id=request.suite_id,
        version=request.version,
        template=template(request.template_id),
        generation=request.generation,
        timeout_seconds=request.timeout_seconds,
        judge=judge,
        judge_generation=request.judge_generation,
        content_sha256="0" * 64,
        registered_at=now,
        registered_by=owner,
    )
    return suite.model_copy(update={"content_sha256": definition_digest(suite)})


def project(row: EvaluationSuiteRow) -> EvaluationSuite:
    suite = EvaluationSuite.model_validate(row.definition)
    if definition_digest(suite) != row.content_sha256 or suite.content_sha256 != row.content_sha256:
        raise EvaluationConflict("Stored suite identity is invalid")
    return suite.model_copy(
        update={"retired": row.retired, "registry_version": row.registry_version}
    )


async def get_row(
    session: AsyncSession, suite_id: str, version: int, *, admit: bool = False
) -> EvaluationSuiteRow:
    row = await session.scalar(
        select(EvaluationSuiteRow)
        .where(EvaluationSuiteRow.suite_id == suite_id, EvaluationSuiteRow.version == version)
        .with_for_update()
    )
    if row is None:
        raise EvaluationNotFound("Evaluation suite was not found")
    if admit and row.retired:
        raise EvaluationConflict("Evaluation suite is retired")
    project(row)
    return row


async def register(
    session: AsyncSession,
    principal: Principal,
    request: EvaluationSuiteRegistration,
    *,
    judge: EvaluationTarget | None,
) -> EvaluationSuite:
    with tracer.start_as_current_span(
        "coire.api.evaluation.register_suite", record_exception=False, set_status_on_exception=False
    ):
        owner = await authorize_live_evaluation_action(session, principal)
        suite = build_suite(request, owner=owner, judge=judge, now=datetime.now(UTC))
        await session.execute(text("SELECT pg_advisory_xact_lock(170001)"))
        existing = await session.scalar(
            select(EvaluationSuiteRow).where(
                EvaluationSuiteRow.suite_id == request.suite_id,
                EvaluationSuiteRow.version == request.version,
            )
        )
        if existing is not None:
            if existing.content_sha256 != suite.content_sha256:
                raise EvaluationConflict(
                    "Suite version already has a different immutable definition"
                )
            return project(existing)
        row = EvaluationSuiteRow(
            id=uuid.uuid4(),
            suite_id=suite.suite_id,
            version=suite.version,
            registry_version=1,
            definition=suite.model_dump(mode="json"),
            content_sha256=suite.content_sha256,
            owner_user_id=owner,
            attribution="admin",
            retired=False,
            created_at=suite.registered_at,
        )
        session.add(row)
        await write_principal_audit(
            session,
            principal=principal,
            action="evaluation.suite.register",
            target_type="evaluation_suite",
            target_id=f"{suite.suite_id}:{suite.version}",
            context={"content_sha256": suite.content_sha256},
        )
        await session.flush()
        return suite


async def retire(
    session: AsyncSession,
    principal: Principal,
    suite_id: str,
    version: int,
    *,
    expected_version: int,
) -> EvaluationSuite:
    await authorize_live_evaluation_action(session, principal)
    row = await get_row(session, suite_id, version)
    if row.registry_version != expected_version:
        raise EvaluationConflict("Suite registry version is stale")
    if not row.retired:
        row.retired = True
        row.registry_version += 1
        await write_principal_audit(
            session,
            principal=principal,
            action="evaluation.suite.retire",
            target_type="evaluation_suite",
            target_id=f"{suite_id}:{version}",
        )
    return project(row)
