"""Explicit evaluation qualification intent and protective resident leases."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    EvaluationCoexistenceProfileRow,
    EvaluationGroupRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    InstanceMemberRow,
    ModelInstanceRow,
    NodeRow,
)
from coire_api.evaluation.authorization import (
    authorize_live_evaluation_action,
    resolve_evaluation_target,
)
from coire_api.evaluation.idempotency import record, replay
from coire_api.evaluation.service import submit
from coire_api.nodes_client import NodeClient
from coire_api.placement.service import acquire_lease, lock_nodes_for_admission, release_lease
from coire_core.errors import EvaluationConflict, EvaluationNotFound, EvaluationValidationError
from coire_core.evaluation_suites.measurement import PROMPTS, prompt_digest
from coire_core.models.evaluation import (
    TERMINAL_EVALUATION_STATES,
    EvaluationControl,
    EvaluationIdentityRequest,
    EvaluationMeasurement,
    EvaluationMeasurementDetail,
    EvaluationMeasurementRequest,
    EvaluationProbePrepare,
    EvaluationProbePrepared,
    EvaluationProbeTarget,
    EvaluationResident,
    EvaluationSubject,
    EvaluationSuite,
)
from coire_core.models.instance import InstanceState
from coire_core.settings import Settings
from coire_scheduler.evaluation_guard import fingerprint, resident_engine_bindings
from coire_scheduler.training_guard import current_residents


def project(row: EvaluationMeasurementRow) -> EvaluationMeasurement:
    if row.report is not None:
        return EvaluationMeasurement.model_validate(row.report)
    return EvaluationMeasurement.model_validate(
        {
            "id": row.id,
            "version": row.version,
            "state": row.state,
            "request": row.request,
            "created_at": row.created_at,
        }
    )


async def detail(
    session: AsyncSession, row: EvaluationMeasurementRow
) -> EvaluationMeasurementDetail:
    measurement = project(row)
    profile = await session.scalar(
        select(EvaluationCoexistenceProfileRow).where(
            EvaluationCoexistenceProfileRow.measurement_id == row.id
        )
    )
    status = "unqualified"
    if measurement.state == "succeeded" and profile is not None:
        status = (
            "invalidated"
            if profile.invalidated_at
            else "expired"
            if profile.expires_at <= datetime.now(UTC)
            else "qualified"
        )
    return EvaluationMeasurementDetail.model_validate(
        {
            **measurement.model_dump(mode="json"),
            "profile_status": status,
            "profile_invalidated_at": profile.invalidated_at if profile else None,
            "profile_invalidated_reason": profile.invalidated_reason if profile else None,
        }
    )


def validate_probe_receipt(
    probes: EvaluationProbePrepared,
    identity: uuid.UUID,
    residents: list[EvaluationResident],
) -> None:
    if (
        probes.measurement_id != identity
        or probes.prompt_set_sha256 != prompt_digest()
        or [item.text for item in probes.prompts] != list(PROMPTS)
        or [item.id for item in probes.prompts]
        != [f"probe-{index}" for index in range(len(PROMPTS))]
        or any(
            set(item.tokens_by_instance) != {resident.instance_id for resident in residents}
            for item in probes.prompts
        )
    ):
        raise EvaluationConflict("Node serving probe receipt differs from its measurement")


async def submit_measurement(
    session: AsyncSession,
    principal: Principal,
    request: EvaluationMeasurementRequest,
    *,
    key: str,
    settings: Settings,
    client: NodeClient,
) -> EvaluationMeasurement:
    owner = await authorize_live_evaluation_action(session, principal)
    existing = await replay(session, owner, "measurement.submit", key, request)
    if existing is not None:
        return EvaluationMeasurement.model_validate(existing.response)
    if request.prompt_set_sha256 != prompt_digest():
        raise EvaluationValidationError("Measurement requires the installed serving probe digest")
    node = await session.scalar(select(NodeRow).where(NodeRow.name == request.node))
    if node is None:
        raise EvaluationNotFound()
    await lock_nodes_for_admission(session, [node.id])
    residents = await current_residents(session, [node.id])
    if residents is None or [(item.instance_id, item.target) for item in residents] != [
        (item.instance_id, item.target)
        for item in sorted(request.resident_targets, key=lambda value: str(value.instance_id))
    ]:
        raise EvaluationConflict("Measurement requires the complete exact live resident set")
    identity = uuid.uuid4()
    prepared_targets: list[EvaluationProbeTarget] = []
    for resident in request.resident_targets:
        resolved = await resolve_evaluation_target(
            session,
            principal,
            EvaluationSubject(
                model_id=resident.target.model_id,
                variant_id=resident.target.variant_id,
                adapter_id=resident.target.adapter_id,
            ),
            client,
        )
        if resolved.target != resident.target or resolved.variant_slug is None:
            raise EvaluationConflict("Resident target identity changed")
        instance = await session.get(ModelInstanceRow, resident.instance_id)
        members = (
            await session.scalars(
                select(InstanceMemberRow).where(
                    InstanceMemberRow.instance_id == resident.instance_id
                )
            )
        ).all()
        if (
            instance is None
            or instance.state is not InstanceState.READY
            or len(members) != 1
            or members[0].node_id != node.id
            or members[0].reservation_id is None
        ):
            raise EvaluationConflict("Measurement requires healthy single-Studio residents")
        prepared_targets.append(
            EvaluationProbeTarget(
                instance_id=resident.instance_id,
                identity=EvaluationIdentityRequest(
                    engine_backend=resolved.engine_backend,
                    target=resolved.target,
                    variant_slug=resolved.variant_slug,
                    template_override=resolved.template_override,
                    capability_profile=resolved.capability_profile,
                ),
            )
        )
    probes = await client.prepare_evaluation_probes(
        node.name,
        EvaluationProbePrepare(
            measurement_id=identity, targets=prepared_targets, prompt_set_sha256=prompt_digest()
        ),
    )
    validate_probe_receipt(probes, identity, request.resident_targets)
    now = datetime.now(UTC)
    row = EvaluationMeasurementRow(
        id=identity,
        owner_user_id=owner,
        request=request.model_dump(mode="json"),
        authorization_snapshot=principal.model_dump(mode="json"),
        version=1,
        state="queued",
        created_at=now,
        deadline_at=now
        + timedelta(
            seconds=settings.evaluation_queue_timeout_seconds + settings.evaluation_timeout_seconds
        ),
    )
    session.add(row)
    await session.flush()
    receipt = await submit(
        session,
        principal,
        request.evaluation,
        idempotency_key=f"measurement-{identity}",
        settings=settings,
        client=client,
        origin="measurement",
    )
    run = await session.get(EvaluationRunRow, receipt.id)
    group = await session.get(EvaluationGroupRow, receipt.group_id)
    assert run is not None and group is not None
    run.measurement_id = identity
    suite = EvaluationSuite.model_validate(run.suite_snapshot)
    phases = len(run.subjects) + (1 if suite.judge else 0)
    if (
        request.requests_per_phase * request.arrival_interval_ms / 1000 * phases + 30
        > suite.timeout_seconds
    ):
        raise EvaluationValidationError(
            "Controlled mixed phases exceed the suite's fixed execution allowance"
        )
    leases: list[str] = []
    for resident in request.resident_targets:
        member = await session.scalar(
            select(InstanceMemberRow).where(InstanceMemberRow.instance_id == resident.instance_id)
        )
        assert member is not None and member.reservation_id is not None
        lease = await acquire_lease(
            session,
            member.reservation_id,
            f"evaluation-measurement:{identity}",
            ttl_seconds=(row.deadline_at - now).total_seconds(),
        )
        leases.append(str(lease.id))
    from coire_core.models.evaluation import EvaluationTarget

    bindings = await resident_engine_bindings(session, request.resident_targets)
    if bindings is None:
        raise EvaluationValidationError("Resident engine identity changed during admission")
    from coire_api.evaluation.inputs import freeze_training_inputs

    training, _input_files = await freeze_training_inputs(session, run)
    row.execution = {
        "run_id": run.id,
        "training": training.model_dump(mode="json") if training else None,
        "phase": "queued",
        "suite": run.suite_snapshot,
        "subjects": run.subjects,
        "probes": probes.model_dump(mode="json"),
        "leases": leases,
        "fingerprint": fingerprint(
            suite,
            [EvaluationTarget.model_validate(value) for value in run.subjects],
            request.resident_targets,
            node,
            bindings,
            training=training,
        ),
        "resident_engines": bindings,
    }
    await session.flush()
    result = project(row)
    await write_principal_audit(
        session,
        principal=principal,
        action="evaluation.measurement.submit",
        target_type="evaluation_measurement",
        target_id=str(identity),
    )
    await record(session, owner, "measurement.submit", key, request, result)
    return result


async def cancel_measurement(
    session: AsyncSession, principal: Principal, identity: uuid.UUID, body: EvaluationControl
) -> EvaluationMeasurement:
    await authorize_live_evaluation_action(session, principal)
    row = await session.get(EvaluationMeasurementRow, identity, with_for_update=True)
    if row is None:
        raise EvaluationNotFound()
    if row.version != body.expected_version:
        raise EvaluationConflict("Measurement version is stale")
    if row.state in {"queued", "running"}:
        row.state, row.version = "cancelled", row.version + 1
        from coire_api.evaluation.execution import stop_children

        run = await session.get(EvaluationRunRow, row.execution.get("run_id"), with_for_update=True)
        if run is not None and run.state not in TERMINAL_EVALUATION_STATES:
            from coire_api.evaluation.events import append

            run.state, run.safe_failure_code = "cancelling", "cancelled"
            run.version += 1
            await stop_children(session, run.id)
            await append(session, run)
        await write_principal_audit(
            session,
            principal=principal,
            action="evaluation.measurement.cancel",
            target_type="evaluation_measurement",
            target_id=str(identity),
        )
    return project(row)


async def release_measurement_leases(session: AsyncSession, row: EvaluationMeasurementRow) -> None:
    run = await session.get(EvaluationRunRow, row.execution.get("run_id"))
    if (
        run is None
        or run.cleanup_state != "complete"
        or run.state not in {"succeeded", "failed", "timed_out", "cancelled"}
    ):
        return
    leases = row.execution.get("leases")
    if isinstance(leases, list):
        for value in leases:
            await release_lease(session, uuid.UUID(str(value)))
    row.execution = {**row.execution, "cleanup": "complete"}
