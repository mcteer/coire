"""Registry-only exact target selection. Public selectors never become store paths."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    TrainingAdapterRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.gateway.telemetry import tracer
from coire_api.registry.service import is_chat_backend, published_ready_entitled
from coire_core.models.acquisition import VariantState
from coire_core.models.adapters import PAIR_PATTERN, InferenceTarget
from coire_core.models.auth import UserRole
from coire_core.models.registry import ModelState


class ModelNotFoundError(Exception):
    """Uniform refusal for unknown, unauthorized or incompatible targets."""


def parse_selector(selector: uuid.UUID | str) -> tuple[uuid.UUID, str | None]:
    if isinstance(selector, uuid.UUID):
        return selector, None
    if re.fullmatch(PAIR_PATTERN, selector):
        base, slug = selector.split("@")
        return uuid.UUID(base), slug
    # UUID compatibility includes existing UUID-only clients, but never path-like strings.
    try:
        return uuid.UUID(selector), None
    except ValueError as exc:
        raise ModelNotFoundError from exc


@dataclass(frozen=True, slots=True)
class RegistryTarget:
    model: ModelRow
    variant: ModelVariantRow | None
    adapter: TrainingAdapterRow | None
    identity: InferenceTarget | None


async def resolve_target(
    session: AsyncSession,
    selector: uuid.UUID | str,
    principal: Principal,
    variant_id: uuid.UUID | None = None,
) -> RegistryTarget:
    with tracer.start_as_current_span("coire.gateway.target.resolve"):
        model_id, slug = parse_selector(selector)
        if principal.kind is PrincipalKind.RUN and (
            model_id not in principal.permitted_model_ids
            or (not principal.permitted_targets and (slug is not None or variant_id is not None))
        ):
            raise ModelNotFoundError
        private_access = principal.is_admin and principal.kind is not PrincipalKind.RUN
        if principal.kind is PrincipalKind.RUN and principal.permitted_targets:
            owner = await session.get(UserRow, principal.user_id) if principal.user_id else None
            if principal.user_id is not None and (owner is None or not owner.active):
                raise ModelNotFoundError
            private_access = owner is not None and owner.active and owner.role is UserRole.ADMIN
        model = await session.get(ModelRow, model_id)
        if model is None or not is_chat_backend(model):
            raise ModelNotFoundError
        if private_access and (model.source or "studio") == "studio":
            if model.state is not ModelState.READY:
                raise ModelNotFoundError
        elif not published_ready_entitled(model, principal.entitlements):
            raise ModelNotFoundError
        adapter = None
        if slug is not None:
            adapter = await session.scalar(
                select(TrainingAdapterRow).where(
                    TrainingAdapterRow.model_id == model_id, TrainingAdapterRow.slug == slug
                )
            )
            if (
                adapter is None
                or adapter.selector != f"{model_id}@{slug}"
                or adapter.state != "ready"
                or adapter.manifest_sha256 is None
                or model.state is not ModelState.READY
                or model.backend != "mlx_lm"
                or (model.source or "studio") != "studio"
                or (variant_id is not None and variant_id != adapter.base_variant_id)
            ):
                raise ModelNotFoundError
            if not private_access and (
                adapter.visibility != "published"
                or not set(adapter.required_entitlements).issubset(principal.entitlements)
            ):
                raise ModelNotFoundError
            variant_id = adapter.base_variant_id
        if (model.source or "studio") != "studio":
            if variant_id is not None or principal.kind is PrincipalKind.RUN:
                raise ModelNotFoundError
            return RegistryTarget(model, None, None, None)
        variant = await session.scalar(
            select(ModelVariantRow)
            .where(
                ModelVariantRow.model_id == model_id,
                ModelVariantRow.id == variant_id
                if variant_id
                else ModelVariantRow.is_default.is_(True),
                ModelVariantRow.validated.is_(True),
                ModelVariantRow.state == VariantState.READY,
            )
            .limit(1)
        )
        if variant is None:
            if variant_id is not None or (
                principal.kind is PrincipalKind.RUN and principal.permitted_targets
            ):
                raise ModelNotFoundError
            return RegistryTarget(model, None, None, None)
        digests = set(
            (
                await session.scalars(
                    select(VariantCopyRow.manifest_sha256).where(
                        VariantCopyRow.variant_id == variant.id, VariantCopyRow.verified.is_(True)
                    )
                )
            ).all()
        )
        digests.discard(None)
        identity = None
        if len(digests) == 1:
            digest = next(iter(digests))
            assert digest is not None
            identity = InferenceTarget(
                model_id=model_id,
                variant_id=variant.id,
                adapter_id=adapter.id if adapter else None,
                base_manifest_sha256=digest,
                adapter_manifest_sha256=adapter.manifest_sha256 if adapter else None,
            )
        if adapter and (
            identity is None or identity.base_manifest_sha256 != adapter.base_manifest_sha256
        ):
            raise ModelNotFoundError
        if (
            principal.kind is PrincipalKind.RUN
            and principal.permitted_targets
            and (identity is None or identity not in principal.permitted_targets)
        ):
            raise ModelNotFoundError
        return RegistryTarget(model, variant, adapter, identity)


async def resolve_validation_target(
    session: AsyncSession, instance_id: uuid.UUID
) -> RegistryTarget:
    """Trusted scheduler-only smoke resolution; never accepts a selector or caller grant.

    The persisted runtime intent, current human authority and immutable extraction
    lineage are all required. This does not grant public inference or harness access.
    """
    from coire_api.training.authorization import authorize_live_training_action
    from coire_api.training.checkpoints import verified_nodes
    from coire_api.training.events import current_job
    from coire_api.training.service import payload_digest, recheck_training_base
    from coire_core.errors import CoireError
    from coire_core.models.instance import InstanceState
    from coire_core.models.training import ResolvedTrainingSpec
    from coire_core.models.training_node import TrainingArtifactManifest
    from coire_core.settings import get_settings

    with tracer.start_as_current_span("coire.gateway.target.validation.resolve"):
        if not get_settings().training_enabled:
            raise ModelNotFoundError
        instance = await session.get(ModelInstanceRow, instance_id, populate_existing=True)
        if instance is None or instance.adapter_id is None:
            raise ModelNotFoundError
        adapter = await session.get(TrainingAdapterRow, instance.adapter_id, populate_existing=True)
        if (
            adapter is None
            or instance.id != uuid.uuid5(adapter.id, "validation-smoke")
            or adapter.metadata_record.get("smoke_instance_id") != str(instance.id)
            or instance.model_id != adapter.model_id
            or instance.variant_id != adapter.base_variant_id
            or instance.policy != "single:auto"
            or instance.state
            in {InstanceState.DRAINING, InstanceState.STOPPED, InstanceState.FAILED}
            or instance.fallback_instance_id is not None
            or instance.fallback_attempted_at is not None
            or adapter.state not in {"validating", "replicating"}
            or adapter.visibility != "admin_only"
            or adapter.verified
            or adapter.selector != f"{adapter.model_id}@{adapter.slug}"
        ):
            raise ModelNotFoundError
        try:
            authority = Principal.model_validate(adapter.metadata_record.get("authority"))
            await authorize_live_training_action(session, authority)
            job = await current_job(session, adapter.source_job_id, lock=True)
            resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
            await recheck_training_base(session, resolved)
            adapter = await session.get(
                TrainingAdapterRow,
                instance.adapter_id,
                populate_existing=True,
                with_for_update=True,
            )
            if (
                adapter is None
                or adapter.source_job_id != job.id
                or adapter.state not in {"validating", "replicating"}
                or adapter.visibility != "admin_only"
                or adapter.verified
                or Principal.model_validate(adapter.metadata_record.get("authority")) != authority
                or adapter.metadata_record.get("smoke_instance_id") != str(instance.id)
                or adapter.model_id != instance.model_id
                or adapter.base_variant_id != instance.variant_id
                or adapter.selector != f"{adapter.model_id}@{adapter.slug}"
            ):
                raise ModelNotFoundError
            manifest = TrainingArtifactManifest.model_validate(
                adapter.metadata_record.get("manifest")
            )
            checkpoint = await session.get(
                TrainingCheckpointRow, adapter.source_checkpoint_id, populate_existing=True
            )
            if checkpoint is None:
                raise ModelNotFoundError
            attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id)
            checkpoint_manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
            if (
                adapter.metadata_record.get("automatic") not in {True, False}
                or (adapter.metadata_record.get("automatic") is True and job.state != "finalizing")
                or (
                    adapter.metadata_record.get("automatic") is True
                    and (
                        checkpoint.fence != job.fence
                        or checkpoint.completed_update != resolved.spec.optim.updates
                        or adapter.slug != job.output_slug
                        or attempt is None
                        or attempt.state != "stopped"
                    )
                )
                or job.model_id != adapter.model_id
                or job.base_variant_id != adapter.base_variant_id
                or adapter.model_id != resolved.spec.model.model_id
                or adapter.base_variant_id != resolved.spec.model.variant_id
                or adapter.base_manifest_sha256 != resolved.base_manifest_sha256
                or adapter.resolved_spec_sha256 != job.resolved_sha256
                or payload_digest(resolved) != job.resolved_sha256
                or checkpoint.job_id != job.id
                or checkpoint.state != "committed"
                or checkpoint.purged_at is not None
                or checkpoint_manifest.kind != "checkpoint"
                or checkpoint_manifest.artifact_id != checkpoint.id
                or checkpoint_manifest.canonical_sha256() != checkpoint.manifest_sha256
                or checkpoint_manifest.total_bytes != checkpoint.total_bytes
                or manifest.kind != "adapter"
                or manifest.artifact_id != adapter.id
                or manifest.job_id != job.id
                or manifest.attempt_id != checkpoint.attempt_id
                or manifest.fence != checkpoint.fence
                or manifest.update != checkpoint.completed_update
                or manifest.runtime_sha256 != resolved.runtime_sha256
                or manifest.resolved_spec_sha256 != job.resolved_sha256
                or manifest.canonical_sha256() != adapter.manifest_sha256
                or checkpoint_manifest.job_id != manifest.job_id
                or checkpoint_manifest.attempt_id != manifest.attempt_id
                or checkpoint_manifest.fence != manifest.fence
                or checkpoint_manifest.update != manifest.update
                or checkpoint_manifest.runtime_sha256 != manifest.runtime_sha256
                or checkpoint_manifest.resolved_spec_sha256 != manifest.resolved_spec_sha256
            ):
                raise ModelNotFoundError
            for artifact in (checkpoint_manifest, manifest):
                if await verified_nodes(
                    session, artifact.artifact_id, artifact.canonical_sha256(), artifact.total_bytes
                ) != {"coire-edge-a", "coire-edge-b"}:
                    raise ModelNotFoundError
        except (ValueError, CoireError) as exc:
            raise ModelNotFoundError from exc
        model = await session.get(ModelRow, adapter.model_id)
        variant = await session.get(ModelVariantRow, adapter.base_variant_id)
        if model is None or variant is None or not variant.validated:
            raise ModelNotFoundError
        return RegistryTarget(
            model,
            variant,
            adapter,
            InferenceTarget(
                model_id=model.id,
                variant_id=variant.id,
                adapter_id=adapter.id,
                base_manifest_sha256=adapter.base_manifest_sha256,
                adapter_manifest_sha256=adapter.manifest_sha256,
            ),
        )
