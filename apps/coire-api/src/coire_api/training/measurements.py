"""Audited measurement intent and immutable Studio input resolution, without estimates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingCommandRow,
    TrainingDatasetAnalysisRow,
    TrainingDatasetRevisionRow,
    TrainingMeasurementRow,
    VariantCopyRow,
)
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.mixtures import compile_mixture
from coire_api.training.service import payload_digest
from coire_api.training.specs import parse_submission
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict, TrainingNotFound, TrainingValidationError
from coire_core.models.acquisition import VariantState
from coire_core.models.audit import AuditOutcome
from coire_core.models.datasets import DatasetAnalysis, DatasetAnalysisBinding, SplitManifest
from coire_core.models.registry import ModelState
from coire_core.models.training import (
    TrainingMeasurementPromptSet,
    TrainingMeasurementReceipt,
    TrainingMeasurementRequest,
    TrainingSubmission,
)
from coire_core.models.training_node import (
    TrainingInputsRequest,
    TrainingMeasurementBinding,
    TrainingMeasurementPrepare,
    TrainingMeasurementSource,
)
from coire_core.settings import Settings
from coire_core.training_data import split_digest


async def freeze_measurement_inputs(
    session: AsyncSession,
    request: TrainingMeasurementRequest,
) -> TrainingMeasurementBinding:
    spec = request.spec
    if spec.placement.preferred_node is not None and request.nodes != [
        spec.placement.preferred_node
    ]:
        raise TrainingValidationError("Probe nodes differ from the recipe's pinned placement")
    model = await session.get(ModelRow, spec.model.model_id, with_for_update=True)
    variant = await session.get(ModelVariantRow, spec.model.variant_id, with_for_update=True)
    if model is None or variant is None or variant.model_id != model.id:
        raise TrainingNotFound()
    if (
        model.state is not ModelState.READY
        or variant.state is not VariantState.READY
        or model.source != "studio"
        or model.kind != "language_model"
        or model.backend != "mlx_lm"
        or variant.backend != "mlx_lm"
    ):
        raise TrainingValidationError("Probe requires an acquired ready local text base")
    copies = {
        name: digest
        for name, digest in (
            await session.execute(
                select(NodeRow.name, VariantCopyRow.manifest_sha256)
                .join(VariantCopyRow, VariantCopyRow.node_id == NodeRow.id)
                .where(VariantCopyRow.variant_id == variant.id, VariantCopyRow.verified.is_(True))
            )
        ).all()
        if name in {"coire-edge-a", "coire-edge-b"}
    }
    if (
        set(copies) != {"coire-edge-a", "coire-edge-b"}
        or len(set(copies.values())) != 1
        or not copies["coire-edge-a"]
    ):
        raise TrainingConflict("Probe base needs matching verified copies on both Studios")
    sources = []
    for dataset_id in sorted(
        {d.dataset_id for d in spec.data.train.datasets} | set(spec.data.validation.dataset_ids),
        key=str,
    ):
        source = await session.get(TrainingDatasetRevisionRow, dataset_id, with_for_update=True)
        if source is None or source.state != "ready" or source.purged_at is not None:
            raise TrainingConflict("Probe dataset needs a successful current analysis")
        expected = DatasetAnalysisBinding.model_validate(
            {
                "dataset_id": source.id,
                "model_id": model.id,
                "variant_id": variant.id,
                "base_manifest_sha256": copies["coire-edge-a"],
                "source_sha256": source.source_sha256,
                "split_sha256": source.split_sha256,
                "format": source.format,
                "model_slug": variant.slug,
                "template_override": model.chat_template,
            }
        )
        analysis = await session.scalar(
            select(TrainingDatasetAnalysisRow)
            .where(
                TrainingDatasetAnalysisRow.dataset_id == source.id,
                TrainingDatasetAnalysisRow.identity_sha256 == payload_digest(expected),
                TrainingDatasetAnalysisRow.state == "succeeded",
            )
            .order_by(
                TrainingDatasetAnalysisRow.created_at.desc(), TrainingDatasetAnalysisRow.id.desc()
            )
        )
        if analysis is None:
            raise TrainingConflict("Exact tokenizer-specific successful analysis is missing")
        command = await session.get(TrainingCommandRow, analysis.command_id)
        if (
            command is None
            or DatasetAnalysisBinding.model_validate(command.payload.get("analysis")) != expected
        ):
            raise TrainingConflict("Frozen analysis binding changed")
        result = DatasetAnalysis.model_validate(analysis.result)
        split = SplitManifest.model_validate(source.split_manifest)
        selected = next((d for d in spec.data.train.datasets if d.dataset_id == dataset_id), None)
        if (
            result.id != analysis.id
            or result.state != "succeeded"
            or result.dataset_id != dataset_id
            or result.model_id != model.id
            or result.variant_id != variant.id
            or result.tokens is None
            or result.tokens.maximum > spec.optim.max_sequence_length
            or (selected is not None and selected.sample_count > len(split.train_rows))
            or split_digest(split) != source.split_sha256
            or split.source_sha256 != source.source_sha256
            or split.dataset_id != dataset_id
            or spec.data.loss_policy
            != ("all_tokens" if source.format == "text" else "final_assistant")
        ):
            raise TrainingConflict("Probe recipe exceeds frozen source/analysis bounds")
        sources.append(TrainingMeasurementSource(binding=expected, split=split, analysis=result))
    by_id = {s.binding.dataset_id: s.split for s in sources}
    compile_mixture(
        spec.data.train,
        by_id,
        validation_manifests=[by_id[key] for key in spec.data.validation.dataset_ids],
    )
    identities = {
        (s.analysis.tokenizer_sha256, s.analysis.template_sha256, s.analysis.runtime_sha256)
        for s in sources
    }
    if len(identities) != 1:
        raise TrainingConflict("Probe source runtime identities disagree")
    tok, template, runtime = identities.pop()
    if tok is None or template is None or runtime is None:
        raise TrainingConflict("Successful analysis has no measured runtime identity")
    base = copies["coire-edge-a"]
    assert base is not None
    return TrainingMeasurementBinding(
        base_manifest_sha256=base,
        tokenizer_sha256=tok,
        template_sha256=template,
        runtime_sha256=runtime,
        worker_version="1",
        sources=sources,
    )


def validate_measurement_prompts(
    request: TrainingMeasurementRequest, prompts: TrainingMeasurementPromptSet
) -> None:
    expected = {t.instance_id for t in request.resident_targets}
    if prompts.canonical_sha256() != request.workload.sha256:
        raise TrainingValidationError("Prompt workload digest differs from frozen recipe")
    if any(
        (p.tokens_by_instance and set(p.tokens_by_instance) != expected)
        or (len(expected) > 1 and not p.tokens_by_instance)
        for p in prompts.prompts
    ):
        raise TrainingValidationError(
            "Tokenizer-specific workload lengths must cover every exact instance"
        )
    if any(
        max(p.tokens_by_instance.get(target, p.input_tokens) for p in prompts.prompts) != 4000
        for target in expected
    ):
        raise TrainingValidationError(
            "Each target's recorded prompt distribution must reach 4000 tokens"
        )


@observed("coire.api.training.measurement.submit")
async def submit_measurement(
    session: AsyncSession,
    principal: Principal,
    request: TrainingMeasurementRequest,
    idempotency_key: str,
    *,
    settings: Settings,
    prompts: TrainingMeasurementPromptSet | None = None,
) -> TrainingMeasurementReceipt:
    owner = await authorize_live_training_action(session, principal)
    if request.prompts is not None:
        if prompts is not None and prompts != request.prompts:
            raise TrainingValidationError(
                "Measurement prompt transport differs from canonical request"
            )
        prompts = request.prompts
    if not settings.training_enabled:
        from coire_core.errors import TrainingUnavailable

        raise TrainingUnavailable("Training is disabled")
    if not 1 <= len(idempotency_key) <= 128:
        raise TrainingValidationError("Measurement idempotency key is out of bounds")
    import yaml

    parse_submission(
        TrainingSubmission(source_yaml=yaml.safe_dump(request.spec.model_dump(mode="json"))),
        settings=settings,
    )
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"coire.measurement.submit:{owner}", 0)
            )
        )
    )
    digest = payload_digest(request)
    prior = await session.scalar(
        select(TrainingCommandRow).where(
            TrainingCommandRow.actor_user_id == owner,
            TrainingCommandRow.operation == "training.measurement",
            TrainingCommandRow.idempotency_key == idempotency_key,
        )
    )
    if prior is not None:
        if prior.request_sha256 != digest:
            raise TrainingConflict("Measurement idempotency key identifies another recipe")
        return TrainingMeasurementReceipt.model_validate(prior.receipt)
    if request.spec.placement.mode == "data_parallel":
        await require_measurement_matrix(session, request, settings)
    if request.mode == "memory" and request.resident_targets:
        raise TrainingValidationError("Memory probes require an isolated accelerator slot")
    if request.mode == "coexistence":
        if prompts is None:
            raise TrainingValidationError(
                "Coexistence requires a frozen Studio-tokenized 4k workload"
            )
        validate_measurement_prompts(request, prompts)
    await session.execute(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("coire.training.measurement.queue", 0))
        )
    )
    binding = await freeze_measurement_inputs(session, request)
    active = await session.scalar(
        select(func.count())
        .select_from(TrainingMeasurementRow)
        .where(TrainingMeasurementRow.state.in_(["queued", "running"]))
    )
    if active is not None and active >= 8:
        raise TrainingConflict("Measurement queue is full")
    owned_active = await session.scalar(
        select(func.count())
        .select_from(TrainingMeasurementRow)
        .where(
            TrainingMeasurementRow.state.in_(["queued", "running"]),
            TrainingMeasurementRow.owner_user_id == owner,
        )
    )
    if owned_active is not None and owned_active >= 4:
        raise TrainingConflict("Administrator measurement queue is full")
    identity = uuid.uuid4()
    receipt = TrainingMeasurementReceipt(measurement_id=identity, state="queued")
    session.add(
        TrainingMeasurementRow(
            id=identity,
            owner_user_id=owner,
            request=request.model_dump(mode="json"),
            state="queued",
            created_at=datetime.now(UTC),
        )
    )
    session.add(
        TrainingCommandRow(
            actor_user_id=owner,
            idempotency_key=idempotency_key,
            operation="training.measurement",
            subject_id=str(identity),
            request_sha256=digest,
            payload={
                "principal": principal.model_dump(mode="json"),
                "binding": binding.model_dump(mode="json"),
                "prompts": prompts.model_dump(mode="json") if prompts else None,
            },
            receipt=receipt.model_dump(mode="json"),
            state="pending",
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="training.measurement.submit",
        target_type="measurement",
        target_id=str(identity),
        outcome=AuditOutcome.OK,
        context={"request_sha256": digest, "mode": request.mode},
    )
    await session.flush()
    return receipt


async def require_measurement_matrix(
    session: AsyncSession, request: TrainingMeasurementRequest, settings: Settings
) -> None:
    """Authenticated code/inventory capability, not a claimed numerical collective pass."""
    from coire_api.nodes_client import NodeClient, NodeError
    from coire_core.errors import TrainingUnavailable
    from coire_core.models.training_node import TrainingMeasurementCapabilities
    from coire_scheduler.training_guard import hardware_digest

    client = NodeClient(settings)
    try:
        for name in request.nodes:
            node = await session.scalar(select(NodeRow).where(NodeRow.name == name))
            if node is None:
                raise TrainingUnavailable("Measurement node inventory is incomplete")
            _, payload = await client._call("GET", name, "/node/training/measurements/capabilities")
            capability = TrainingMeasurementCapabilities.model_validate(payload)
            if (
                capability.node != name
                or capability.hardware_sha256 != hardware_digest(node)
                or 2 not in capability.world_sizes
                or not capability.measurement_checkpoint
            ):
                raise TrainingUnavailable(
                    "Two-rank measurement callback or declared launch inventory is unavailable"
                )
    except (NodeError, ValueError):
        raise TrainingUnavailable(
            "Current authenticated measurement matrix capability is unavailable"
        ) from None
    finally:
        await client.aclose()


async def mint_measurement_inputs(
    session: AsyncSession,
    principal: Principal,
    probe: TrainingMeasurementPrepare,
    *,
    settings: Settings,
) -> TrainingInputsRequest:
    """Secrets stay in transport; only their hashes are stored in audited command metadata."""
    import hashlib
    import secrets
    from datetime import timedelta

    from coire_core.models.training_node import (
        DatasetInputGrant,
        TrainingInputSource,
        TrainingInputsRequest,
        TrainingMeasurementDispatch,
    )

    owner = await authorize_live_training_action(session, principal)
    row = await session.get(TrainingMeasurementRow, probe.measurement_id)
    command = await session.scalar(
        select(TrainingCommandRow).where(
            TrainingCommandRow.operation == "training.measurement",
            TrainingCommandRow.subject_id == str(probe.measurement_id),
        )
    )
    if row is None or command is None or row.state != "running" or row.owner_user_id != owner:
        raise TrainingConflict("Measurement source authority ended")
    dispatch = TrainingMeasurementDispatch.model_validate(command.payload.get("dispatch"))
    if probe not in dispatch.commands:
        raise TrainingConflict("Measurement input request differs from pinned dispatch")
    node = await session.scalar(select(NodeRow).where(NodeRow.name == probe.prepare.node))
    if node is None:
        raise TrainingNotFound()
    sources = []
    expiry = min(
        probe.deadline, datetime.now(UTC) + timedelta(seconds=settings.training_transfer_grant_s)
    )
    for frozen in dispatch.sources:
        source = await session.get(TrainingDatasetRevisionRow, frozen.binding.dataset_id)
        if (
            source is None
            or source.state != "ready"
            or source.source_sha256 != frozen.binding.source_sha256
        ):
            raise TrainingConflict("Measurement source was retired or changed")
        secret = secrets.token_urlsafe(32)
        grant_id = uuid.uuid4()
        session.add(
            TrainingCommandRow(
                id=grant_id,
                actor_user_id=owner,
                idempotency_key=str(grant_id),
                operation="measurement.input.grant",
                subject_id=str(probe.measurement_id),
                request_sha256=hashlib.sha256(secret.encode()).hexdigest(),
                payload={
                    "node": probe.prepare.node,
                    "attempt_id": probe.prepare.attempt_id,
                    "dataset_id": str(source.id),
                    "source_sha256": source.source_sha256,
                    "max_bytes": source.source_bytes,
                    "expires_at": expiry.isoformat(),
                },
                state="succeeded",
            )
        )
        sources.append(
            TrainingInputSource(
                binding=frozen.binding,
                split=frozen.split,
                analysis=frozen.analysis,
                grant=DatasetInputGrant(
                    grant_id=grant_id,
                    node=probe.prepare.node,
                    dataset_id=source.id,
                    source_sha256=source.source_sha256,
                    max_bytes=source.source_bytes,
                    attempt_id=probe.prepare.attempt_id,
                    expires_at=expiry,
                    secret=secret,
                ),
            )
        )
    return TrainingInputsRequest(
        **{
            k: getattr(probe.prepare, k)
            for k in (
                "job_id",
                "attempt_id",
                "fence",
                "request_sha256",
                "node",
                "rank",
                "world_size",
            )
        },
        command_id=uuid.uuid4(),
        lease_expires_at=probe.prepare.lease_expires_at,
        sources=sources,
    )


async def authorized_measurement_source(
    session: AsyncSession,
    dataset_id: uuid.UUID,
    node_name: str,
    secret: str,
) -> TrainingDatasetRevisionRow:
    """Delegate from the authenticated internal dataset route for measurement-only grants."""
    import hashlib
    import hmac

    from coire_core.models.training_node import TrainingMeasurementDispatch

    if not 32 <= len(secret) <= 256 or not secret.isascii():
        raise TrainingNotFound()
    digest = hashlib.sha256(secret.encode()).hexdigest()
    grant = await session.scalar(
        select(TrainingCommandRow).where(
            TrainingCommandRow.operation == "measurement.input.grant",
            TrainingCommandRow.request_sha256 == digest,
        )
    )
    if grant is None or not hmac.compare_digest(grant.request_sha256, digest):
        raise TrainingNotFound()
    row = await session.get(TrainingMeasurementRow, uuid.UUID(grant.subject_id))
    command = await session.scalar(
        select(TrainingCommandRow).where(
            TrainingCommandRow.operation == "training.measurement",
            TrainingCommandRow.subject_id == grant.subject_id,
        )
    )
    if row is None or row.state != "running" or command is None:
        raise TrainingNotFound()
    await authorize_live_training_action(
        session, Principal.model_validate(command.payload["principal"])
    )
    dispatch = TrainingMeasurementDispatch.model_validate(command.payload.get("dispatch"))
    scope = grant.payload
    if (
        scope.get("node") != node_name
        or scope.get("dataset_id") != str(dataset_id)
        or datetime.fromisoformat(str(scope["expires_at"])) <= datetime.now(UTC)
        or not any(
            c.prepare.attempt_id == scope.get("attempt_id")
            and c.prepare.node == node_name
            and c.deadline > datetime.now(UTC)
            for c in dispatch.commands
        )
    ):
        raise TrainingNotFound()
    source = await session.get(TrainingDatasetRevisionRow, dataset_id, with_for_update=True)
    if (
        source is None
        or source.state != "ready"
        or source.purged_at is not None
        or source.source_sha256 != scope.get("source_sha256")
        or source.source_bytes != scope.get("max_bytes")
    ):
        raise TrainingNotFound()
    return source
