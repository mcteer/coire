"""Immutable, content-addressed authority for upgrade and recovery boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from coire_core.models.files import SHA256_PATTERN, SLUG_PATTERN, ULID_PATTERN

Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
UpgradeId = Annotated[str, Field(pattern=ULID_PATTERN)]
ArtifactKey = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")]
ServiceName = Literal[
    "coire-file-worker", "coire-ops", "coire-mcp", "coire-scheduler", "coire-api", "coire-web"
]
StudioName = Literal["coire-edge-a", "coire-edge-b"]
MaintenanceTarget = Literal["cluster", "core", StudioName, ServiceName]
COHORT_ORDER: tuple[ServiceName, ...] = (
    "coire-file-worker",
    "coire-ops",
    "coire-mcp",
    "coire-scheduler",
    "coire-api",
    "coire-web",
)
SchemaHead = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_]{1,64}$")]


class UpgradeWire(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )

    def sha256(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class UpgradeKind(StrEnum):
    STUDIO = "studio"
    CONTROL_PLANE = "control_plane"


class UpgradeAction(StrEnum):
    UPGRADE = "upgrade"
    ROLLBACK = "rollback"
    BOOTSTRAP_HANDOVER = "bootstrap_handover"


class PreparedNativeRuntime(UpgradeWire):
    """Private preparation receipt lives outside the sealed runtime graph."""

    environment_id: ArtifactKey
    environment_sha256: Sha256
    interpreter_id: ArtifactKey
    interpreter_sha256: Sha256


class NativeRuntimeSelection(UpgradeWire):
    """Independent source selection survives candidate failure and checkout removal."""

    protocol: Literal[1] = 1
    node: StudioName
    release_sha256: Sha256
    runtime: PreparedNativeRuntime
    generation: int = Field(ge=0, le=2**63 - 1)
    job_id: UpgradeId | None = None
    selected_at: AwareDatetime

    @model_validator(mode="after")
    def selection_authority(self) -> NativeRuntimeSelection:
        if (self.job_id is None) != (self.generation == 0):
            raise ValueError("native selection operation fence is incomplete")
        return self


class UpgradeDiskHold(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    resource_key: ArtifactKey
    state: Literal["allocating", "held", "releasing", "released"]
    requested_bytes: int = Field(ge=1, le=2**63 - 1)
    allocated_bytes: int = Field(default=0, ge=0, le=2**63 - 1)
    file_device: int | None = Field(default=None, ge=0, le=2**64 - 1)
    file_inode: int | None = Field(default=None, ge=1, le=2**64 - 1)
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def allocation_identity(self) -> UpgradeDiskHold:
        if (self.file_device is None) != (self.file_inode is None):
            raise ValueError("disk reservation file identity is incomplete")
        if self.state == "held" and (
            self.file_device is None or self.allocated_bytes < self.requested_bytes
        ):
            raise ValueError("disk reservation is not positively allocated")
        return self


class UpgradePhase(StrEnum):
    ACCEPTED = "accepted"
    PREFLIGHT = "preflight"
    FENCING = "fencing"
    DRAINING = "draining"
    STAGED = "staged"
    SMOKE_BEFORE = "smoke_before"
    MIGRATING = "migrating"
    ACTIVATING = "activating"
    VERIFYING = "verifying"
    QUALIFYING = "qualifying"
    COMMITTING = "committing"
    RESTORING = "restoring"
    VERIFYING_SOURCE = "verifying_source"
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"
    FAILED_UNCHANGED = "failed_unchanged"
    CANCELLED_UNCHANGED = "cancelled_unchanged"
    ROLLED_BACK = "rolled_back"


TERMINAL_UPGRADE_PHASES = frozenset(
    {
        UpgradePhase.SUCCEEDED,
        UpgradePhase.FAILED_UNCHANGED,
        UpgradePhase.CANCELLED_UNCHANGED,
        UpgradePhase.ROLLED_BACK,
    }
)


class UpgradeTiming(UpgradeWire):
    drain_s: int = Field(default=900, ge=1, le=900)
    readiness_s: int = Field(default=120, ge=1, le=120)
    recovery_s: int = Field(default=300, ge=1, le=300)
    gateway_outage_s: int = Field(default=300, ge=1, le=300)


class UpgradeCompatibility(UpgradeWire):
    source_sha256s: tuple[Sha256, ...] = Field(min_length=1, max_length=32)
    readable_schema_heads: tuple[SchemaHead, ...] = Field(min_length=1, max_length=32)
    native_journal_versions: tuple[Annotated[int, Field(ge=1, le=100)], ...] = Field(
        default=(3,), min_length=1, max_length=16
    )
    privacy_semantics: Literal[1] = 1
    bootstrap_protocol: Literal[1] = 1
    evidence_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def unique_compatibility(self) -> UpgradeCompatibility:
        if len(set(self.native_journal_versions)) != len(self.native_journal_versions):
            raise ValueError("duplicate native journal compatibility")
        if len(set(self.source_sha256s)) != len(self.source_sha256s):
            raise ValueError("duplicate source compatibility")
        if len(set(self.readable_schema_heads)) != len(self.readable_schema_heads):
            raise ValueError("duplicate schema compatibility")
        return self


class UpgradeDockerImage(UpgradeWire):
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    os: Literal["linux"]
    architecture: Literal["arm64"]


class UpgradeServiceSpec(UpgradeWire):
    service: ServiceName
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    configuration: dict[str, JsonValue]
    configuration_sha256: Sha256

    @model_validator(mode="after")
    def verified_configuration(self) -> UpgradeServiceSpec:
        payload = json.dumps(self.configuration, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(payload).hexdigest() != self.configuration_sha256:
            raise ValueError("configuration digest mismatch")
        return self


class UpgradeReleaseManifest(UpgradeWire):
    protocol: Literal[1] = 1
    kind: UpgradeKind
    version: str = Field(min_length=1, max_length=100)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    platform: Literal["macos-arm64", "linux-arm64"]
    prepared_at: AwareDatetime
    compatibility: UpgradeCompatibility
    artifact_sha256s: dict[ArtifactKey, Sha256] = Field(min_length=1, max_length=256)
    staged_bytes: int = Field(ge=1, le=2**63 - 1)
    environment_id: ArtifactKey | None = None
    environment_sha256: Sha256 | None = None
    project: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    services: tuple[UpgradeServiceSpec, ...] = Field(default=(), max_length=6)
    schema_head: SchemaHead | None = None
    compose_sha256: Sha256 | None = None
    host_release_key: ArtifactKey | None = None
    paired_required: bool = False

    @model_validator(mode="after")
    def target_shape(self) -> UpgradeReleaseManifest:
        if self.kind is UpgradeKind.STUDIO:
            if (
                self.platform != "macos-arm64"
                or self.environment_id is None
                or self.environment_sha256 is None
                or self.project is not None
                or self.services
                or self.host_release_key is not None
            ):
                raise ValueError("Studio manifest target differs")
        elif (
            self.platform != "linux-arm64"
            or self.project is None
            or not self.services
            or self.schema_head is None
            or self.compose_sha256 is None
            or self.environment_id is not None
            or self.environment_sha256 is not None
        ):
            raise ValueError("control-plane manifest target differs")
        if len({s.service for s in self.services}) != len(self.services):
            raise ValueError("duplicate service")
        if tuple(s.service for s in self.services) != tuple(
            service for service in COHORT_ORDER if any(s.service == service for s in self.services)
        ):
            raise ValueError("service cohort order differs")
        return self


class UpgradeReleaseRegister(UpgradeWire):
    prepared_artifact_id: ArtifactKey
    expected_sha256: Sha256


class UpgradeRelease(UpgradeWire):
    release_id: UUID
    manifest_sha256: Sha256
    manifest: UpgradeReleaseManifest
    prepared_artifact_id: ArtifactKey
    prepared_by: UUID
    status: Literal["prepared", "invalid", "retired"] = "prepared"

    @model_validator(mode="after")
    def content_identity(self) -> UpgradeRelease:
        if self.manifest.sha256() != self.manifest_sha256:
            raise ValueError("release manifest identity differs")
        return self


class UpgradeReleaseSummary(UpgradeWire):
    """Public release metadata excludes executor configuration and private mount paths."""

    release_id: UUID
    manifest_sha256: Sha256
    kind: UpgradeKind
    version: str = Field(min_length=1, max_length=100)
    platform: Literal["macos-arm64", "linux-arm64"]
    prepared_at: AwareDatetime
    prepared_by: UUID
    status: Literal["prepared", "invalid", "retired"]
    compatibility: UpgradeCompatibility
    staged_bytes: int = Field(ge=1, le=2**63 - 1)
    services: tuple[ServiceName, ...] = Field(max_length=6)
    paired_required: bool

    @classmethod
    def from_release(cls, release: UpgradeRelease) -> UpgradeReleaseSummary:
        manifest = release.manifest
        return cls(
            release_id=release.release_id,
            manifest_sha256=release.manifest_sha256,
            kind=manifest.kind,
            version=manifest.version,
            platform=manifest.platform,
            prepared_at=manifest.prepared_at,
            prepared_by=release.prepared_by,
            status=release.status,
            compatibility=manifest.compatibility,
            staged_bytes=manifest.staged_bytes,
            services=tuple(s.service for s in manifest.services),
            paired_required=manifest.paired_required,
        )


def release_pair_compatible(
    source: UpgradeRelease, target: UpgradeRelease, action: UpgradeAction, parent_job_id: str | None
) -> bool:
    """A rollback reverses a recorded forward pair, avoiding circular manifest digests.

    The authoritative service additionally verifies the successful parent operation.
    Declared persistent format support is required in either direction.
    """
    if (
        source.manifest.kind is not target.manifest.kind
        or source.manifest.platform != target.manifest.platform
        or source.manifest.project != target.manifest.project
        or 3 not in source.manifest.compatibility.native_journal_versions
        or 3 not in target.manifest.compatibility.native_journal_versions
    ):
        return False
    if action is UpgradeAction.ROLLBACK:
        return (
            parent_job_id is not None
            and target.manifest_sha256 in source.manifest.compatibility.source_sha256s
        )
    return source.manifest_sha256 in target.manifest.compatibility.source_sha256s


class UpgradeReleasePage(UpgradeWire):
    items: tuple[UpgradeReleaseSummary, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=128)


class BackupEvidence(UpgradeWire):
    backup_id: ArtifactKey
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    schema_head: SchemaHead
    created_at: AwareDatetime
    sha256_digest: Sha256
    size_bytes: int = Field(ge=1, le=2**63 - 1)
    validated_at: AwareDatetime
    validation_sha256: Sha256


class UpgradeBackupValidation(UpgradeWire):
    """Trusted preparation receipt for a restore into an isolated disposable database."""

    protocol: Literal[1] = 1
    backup_id: ArtifactKey
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    schema_head: SchemaHead
    schema_sha256: Sha256
    dump_sha256: Sha256
    size_bytes: int = Field(ge=1, le=2**63 - 1)
    restore_container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    restore_container_name: str = Field(pattern=r"^coire-upgrade-backup-[a-f0-9]{32}$")
    postgres_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    restored_at: AwareDatetime
    restore_exit_code: Literal[0] = 0
    schema_verified: Literal[True] = True


class UpgradeBackupPublication(UpgradeWire):
    protocol: Literal[1] = 1
    evidence: BackupEvidence
    validation: UpgradeBackupValidation


class UpgradeStartRequest(UpgradeWire):
    release_id: UUID
    expected_source_sha256: Sha256
    idempotency_key: UUID
    acknowledge_maintenance: Literal[True]


class UpgradeRollbackRequest(UpgradeWire):
    idempotency_key: UUID
    expected_source_sha256: Sha256
    acknowledge_maintenance: Literal[True]


class UpgradeCancelRequest(UpgradeWire):
    reason: str = Field(default="administrator_cancelled", min_length=1, max_length=256)


class UpgradeOperation(UpgradeWire):
    job_id: UpgradeId
    action: UpgradeAction
    kind: UpgradeKind
    node: StudioName | None = None
    source_release_id: UUID
    target_release_id: UUID
    source_sha256: Sha256
    target_sha256: Sha256
    user_id: UUID
    key_id: UUID | None = None
    idempotency_key: UUID
    request_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    cohort: tuple[ServiceName, ...] = Field(default=(), max_length=6)
    timing: UpgradeTiming = Field(default_factory=UpgradeTiming)
    phase: UpgradePhase = UpgradePhase.ACCEPTED
    accepted_at: AwareDatetime
    updated_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    cancel_requested: bool = False
    cancellation_allowed: bool = True
    reason: str | None = Field(default=None, max_length=256)
    receipt_sequence: int = Field(default=0, ge=0, le=2**63 - 1)
    parent_job_id: UpgradeId | None = None
    retained_schema_head: SchemaHead | None = None

    def authority_sha256(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={
                "phase",
                "updated_at",
                "finished_at",
                "cancel_requested",
                "cancellation_allowed",
                "reason",
                "receipt_sequence",
                "retained_schema_head",
            },
        )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @model_validator(mode="after")
    def coherent_operation(self) -> UpgradeOperation:
        if (self.kind is UpgradeKind.STUDIO) != (self.node is not None):
            raise ValueError("operation target kind differs")
        if self.source_sha256 == self.target_sha256:
            raise ValueError("release is already selected")
        if (self.phase in TERMINAL_UPGRADE_PHASES) != (self.finished_at is not None):
            raise ValueError("terminal operation time differs")
        if len(set(self.cohort)) != len(self.cohort):
            raise ValueError("duplicate cohort service")
        if self.phase in TERMINAL_UPGRADE_PHASES and self.cancellation_allowed:
            # Normalize during validation without invoking the frozen model's
            # public assignment path or returning a replacement from __init__.
            object.__setattr__(self, "cancellation_allowed", False)
        return self


class UpgradeQualification(UpgradeWire):
    capability: Literal["serving", "paired", "training", "evaluation", "image"]
    state: Literal["unavailable", "stale", "qualified"]
    runtime_sha256: Sha256 | None = None
    peer_runtime_sha256: Sha256 | None = None
    reason: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")

    @model_validator(mode="after")
    def exact_qualification(self) -> UpgradeQualification:
        if self.state == "qualified" and (
            self.runtime_sha256 is None
            or (self.capability == "paired" and self.peer_runtime_sha256 is None)
        ):
            raise ValueError("qualification requires exact runtime identities")
        if self.capability != "paired" and self.peer_runtime_sha256 is not None:
            raise ValueError("peer identity applies only to paired qualification")
        return self


class UpgradeTargetStatus(UpgradeWire):
    kind: UpgradeKind
    node: StudioName | None = None
    desired_sha256: Sha256 | None = None
    observed_sha256: Sha256 | None = None
    retained_sha256: Sha256 | None = None
    release_id: UUID | None = None
    readiness: Literal["ready", "maintenance", "recovering", "not_ready", "drifted"]
    blocking_reasons: tuple[Annotated[str, Field(max_length=256)], ...] = Field(
        default=(), max_length=32
    )
    observed_at: AwareDatetime
    bootstrap_ready: bool = False
    rollback_eligible: bool = False
    rollback_job_id: UpgradeId | None = None
    rollback_blocking_reasons: tuple[Annotated[str, Field(max_length=256)], ...] = Field(
        default=(), max_length=16
    )
    qualifications: tuple[UpgradeQualification, ...] = Field(default=(), max_length=5)
    service_image_ids: dict[
        ServiceName, Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    ] = Field(default_factory=dict, max_length=6)

    @model_validator(mode="after")
    def honest_readiness(self) -> UpgradeTargetStatus:
        if (self.kind is UpgradeKind.STUDIO) != (self.node is not None):
            raise ValueError("target node differs from target kind")
        if len({item.capability for item in self.qualifications}) != len(self.qualifications):
            raise ValueError("target qualification capabilities are duplicated")
        if self.readiness == "ready" and (
            not self.bootstrap_ready
            or self.release_id is None
            or self.desired_sha256 is None
            or self.desired_sha256 != self.observed_sha256
            or bool(self.blocking_reasons)
        ):
            raise ValueError("ready target requires verified bootstrap and exact selected identity")
        if self.rollback_eligible and self.retained_sha256 is None:
            raise ValueError("rollback eligibility requires retained source identity")
        return self


class UpgradeTargetList(UpgradeWire):
    items: tuple[UpgradeTargetStatus, ...] = Field(max_length=3)


class UpgradeMaintenance(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    targets: tuple[MaintenanceTarget, ...] = Field(min_length=1, max_length=16)
    state: Literal["fencing", "draining", "active", "recovering", "released"]
    updated_at: AwareDatetime
    drain_deadline_at: AwareDatetime
    initiator_user_id: UUID | None = None
    acknowledged_generations: dict[MaintenanceTarget, Annotated[int, Field(ge=1, le=2**63 - 1)]] = (
        Field(default_factory=dict, max_length=16)
    )

    @model_validator(mode="after")
    def matching_acknowledgments(self) -> UpgradeMaintenance:
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("maintenance contains duplicate targets")
        if any(
            target not in self.targets or generation != self.generation
            for target, generation in self.acknowledged_generations.items()
        ):
            raise ValueError("maintenance acknowledgment differs")
        return self

    def matches_plan(self, plan: UpgradeExecutionPlan) -> bool:
        return self.state != "released" and (
            self.job_id,
            self.generation,
            self.targets,
            self.drain_deadline_at,
            self.initiator_user_id,
        ) == (
            plan.operation.job_id,
            plan.operation.generation,
            plan.maintenance.targets,
            plan.maintenance.drain_deadline_at,
            plan.maintenance.initiator_user_id,
        )


class NodeMaintenanceCommand(UpgradeWire):
    maintenance: UpgradeMaintenance
    action: Literal["apply", "reconcile", "release"] = "apply"
    terminal_verified: bool = False
    source_unchanged: bool = False
    source_runtime_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def release_proof(self) -> NodeMaintenanceCommand:
        if self.action == "release" and not self.terminal_verified:
            raise ValueError("maintenance release requires verified terminal reconciliation")
        if self.source_unchanged and (
            self.action != "release"
            or not self.terminal_verified
            or self.source_runtime_sha256 is None
        ):
            raise ValueError("unchanged-source release requires terminal runtime identity")
        if not self.source_unchanged and self.source_runtime_sha256 is not None:
            raise ValueError("source identity requires unchanged-source release")
        return self


class NodeMaintenanceAck(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    node: StudioName
    applied_at: AwareDatetime
    released: bool = False


class UpgradeInventoryEntry(UpgradeWire):
    owner: ArtifactKey
    kind: ArtifactKey
    work_id: str = Field(min_length=1, max_length=128)
    state: Literal["active", "cleaning", "standing", "unknown"]
    holds: tuple[Annotated[str, Field(max_length=128)], ...] = Field(default=(), max_length=256)
    process_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    process_created_at: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def owned_process_identity(self) -> UpgradeInventoryEntry:
        if self.state != "unknown" and (self.process_id is None) != (
            self.process_created_at is None
        ):
            raise ValueError("inventory process identity is incomplete")
        if self.state == "standing" and self.process_id is not None:
            raise ValueError("standing reservations cannot claim active processes are idle")
        return self


class UpgradeOwnerInventory(UpgradeWire):
    owner: ArtifactKey
    status: Literal["complete", "blocked", "unknown"]
    entries: tuple[UpgradeInventoryEntry, ...] = Field(default=(), max_length=4096)
    reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def matching_entries(self) -> UpgradeOwnerInventory:
        if any(entry.owner != self.owner for entry in self.entries):
            raise ValueError("inventory entry belongs to another owner")
        if len({(entry.kind, entry.work_id) for entry in self.entries}) != len(self.entries):
            raise ValueError("inventory contains duplicate work")
        return self


NODE_UPGRADE_OWNERS = frozenset(
    {
        "requests",
        "engines",
        "images",
        "training",
        "training-lifecycle",
        "measurements",
        "analysis",
        "artifacts",
        "runs",
        "acquisition",
        "shards",
        "probes",
        "reservations",
        "failover",
    }
)


class NodeUpgradeInventory(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    node: StudioName
    runtime_sha256: Sha256
    observed_at: AwareDatetime
    owners: tuple[UpgradeOwnerInventory, ...] = Field(max_length=64)

    def idle(self, required_owners: frozenset[str]) -> bool:
        if {o.owner for o in self.owners} != required_owners:
            return False
        if len(self.owners) != len(required_owners):
            return False
        return all(
            o.status == "complete" and all(e.state == "standing" for e in o.entries)
            for o in self.owners
        )

    def reconciled(self, required_owners: frozenset[str]) -> bool:
        """Unchanged-source abort may preserve known work, never unknown ownership."""
        return (
            {owner.owner for owner in self.owners} == required_owners
            and len(self.owners) == len(required_owners)
            and all(
                owner.status == "complete"
                and all(entry.state != "unknown" for entry in owner.entries)
                for owner in self.owners
            )
        )


class UpgradeAdmissionStatus(UpgradeWire):
    allowed: bool


class CoreUpgradeInventory(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    release_sha256: Sha256
    observed_at: AwareDatetime
    owners: tuple[UpgradeOwnerInventory, ...] = Field(max_length=64)

    def idle(self, required_owners: frozenset[str]) -> bool:
        return (
            len(self.owners) == len(required_owners)
            and {owner.owner for owner in self.owners} == required_owners
            and all(
                owner.status == "complete"
                and all(entry.state == "standing" for entry in owner.entries)
                for owner in self.owners
            )
        )


class UpgradeSchemaObservation(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    schema_head: SchemaHead
    schema_sha256: Sha256
    observed_at: AwareDatetime


class UpgradeSmokeRequest(UpgradeWire):
    generation: int = Field(ge=1, le=2**63 - 1)
    check: Literal["gateway", "node", "collective", "source"]


class UpgradeSmokeResult(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    check: Literal["gateway", "node", "collective", "source"]
    runtime_sha256: Sha256
    model_id: UUID
    model_variant_id: UUID
    model_manifest_sha256: Sha256
    generated_tokens: int = Field(ge=0, le=64)
    passed: bool
    started_at: AwareDatetime
    finished_at: AwareDatetime
    output_sha256: Sha256
    peer_runtime_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def real_smoke(self) -> UpgradeSmokeResult:
        if self.finished_at < self.started_at:
            raise ValueError("smoke time differs")
        if self.passed and self.check != "collective" and self.generated_tokens < 10:
            raise ValueError("passing smoke requires ten generated tokens")
        if self.check == "collective" and self.peer_runtime_sha256 is None:
            raise ValueError("collective requires exact peer identity")
        return self


class UpgradeExecutionPlan(UpgradeWire):
    protocol: Literal[1] = 1
    operation: UpgradeOperation
    source: UpgradeRelease
    target: UpgradeRelease
    maintenance: UpgradeMaintenance
    authorization_expires_at: AwareDatetime
    model_id: UUID
    model_variant_id: UUID
    model_slug: Annotated[str, Field(pattern=SLUG_PATTERN, max_length=255)]
    model_manifest_sha256: Sha256
    peer_runtime_sha256: Sha256 | None = None
    backup: BackupEvidence | None = None
    initial_schema_head: SchemaHead | None = None
    initial_schema_sha256: Sha256 | None = None
    schema_head_after: SchemaHead | None = None

    @property
    def needs_migration(self) -> bool:
        return self.initial_schema_head != self.schema_head_after

    def authority_sha256(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={
                "authorization_expires_at",
                "activation_grant",
                "activation_receipt",
                "peer_inventory",
            },
        )
        payload["operation"] = self.operation.authority_sha256()
        payload["maintenance"] = self.maintenance.model_dump(
            mode="json",
            exclude={
                "state",
                "updated_at",
                "acknowledged_generations",
            },
        )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @model_validator(mode="after")
    def bound_authority(self) -> UpgradeExecutionPlan:
        op = self.operation
        if (
            op.source_release_id != self.source.release_id
            or op.target_release_id != self.target.release_id
            or op.source_sha256 != self.source.manifest_sha256
            or op.target_sha256 != self.target.manifest_sha256
            or op.kind is not self.source.manifest.kind
            or op.kind is not self.target.manifest.kind
            or op.job_id != self.maintenance.job_id
            or op.generation != self.maintenance.generation
        ):
            raise ValueError("execution authority differs")
        schema_identity = (
            self.initial_schema_head,
            self.initial_schema_sha256,
            self.schema_head_after,
        )
        if op.kind is UpgradeKind.CONTROL_PLANE:
            if any(item is None for item in schema_identity):
                raise ValueError("Core execution requires actual schema identity")
            if (
                self.initial_schema_head
                not in self.source.manifest.compatibility.readable_schema_heads
            ):
                raise ValueError("source cannot read deployed schema")
            if any(
                self.schema_head_after not in release.manifest.compatibility.readable_schema_heads
                for release in (self.source, self.target)
            ):
                raise ValueError("runtime pair cannot read retained schema")
            if op.action is UpgradeAction.ROLLBACK and self.needs_migration:
                raise ValueError("rollback retains the deployed schema")
        elif any(item is not None for item in schema_identity):
            raise ValueError("Studio execution has no database schema authority")
        if self.maintenance.state == "released":
            raise ValueError("execution maintenance is released")
        if (
            self.maintenance.initiator_user_id is not None
            and self.maintenance.initiator_user_id != op.user_id
        ):
            raise ValueError("maintenance initiator differs")
        if (
            op.node is not None
            and op.node not in self.maintenance.targets
            and "cluster" not in self.maintenance.targets
        ):
            raise ValueError("execution node is not fenced")
        if self.source.manifest.project != self.target.manifest.project:
            raise ValueError("execution project differs")
        if not release_pair_compatible(self.source, self.target, op.action, op.parent_job_id):
            raise ValueError("source compatibility absent")
        if (
            self.source.manifest.paired_required or self.target.manifest.paired_required
        ) and self.peer_runtime_sha256 is None:
            raise ValueError("paired release needs exact peer identity")
        return self


def core_upgrade_required_owners(plan: UpgradeExecutionPlan) -> frozenset[str]:
    """Fixed owner map shared by the gateway's inventory and independent executor."""
    if plan.operation.kind is not UpgradeKind.CONTROL_PLANE:
        raise ValueError("Core inventory requires a Core execution plan")
    if "core" in plan.maintenance.targets:
        return frozenset(
            {
                "requests",
                "scheduler",
                "runs",
                "mcp",
                "ops",
                "feedback",
                "files",
                "renewals",
                "failover",
            }
        )
    owners = {"requests"}
    for service, owner in (
        ("coire-mcp", "mcp"),
        ("coire-ops", "ops"),
        ("coire-file-worker", "files"),
    ):
        if service in plan.maintenance.targets:
            owners.add(owner)
    return frozenset(owners)


class CoreNodeSmokeScope(UpgradeWire):
    """A fixed registry serving request under one accepted Core maintenance owner."""

    plan: UpgradeExecutionPlan
    node: StudioName
    engine_id: UUID
    runtime_sha256: Sha256
    deadline_at: AwareDatetime
    check: Literal["gateway", "source"] = "gateway"

    @model_validator(mode="after")
    def core_only(self) -> CoreNodeSmokeScope:
        if self.plan.operation.kind is not UpgradeKind.CONTROL_PLANE:
            raise ValueError("Core gateway smoke scope requires an accepted Core operation")
        return self


class CoreNodeSmokeLifecycle(UpgradeWire):
    scope: CoreNodeSmokeScope
    state: Literal["active", "revoked", "released"] = "active"
    updated_at: AwareDatetime


class CoreNodeSmokeCleanupReceipt(UpgradeWire):
    scope_sha256: Sha256
    engine_id: UUID
    runtime_sha256: Sha256
    cleanup_verified: Literal[True] = True
    observed_at: AwareDatetime


class UpgradeEvidence(UpgradeWire):
    """Diagnostic evidence carries verified identities and outcomes, never raw logs/input."""

    runtime_sha256: Sha256 | None = None
    release_sha256: Sha256 | None = None
    schema_head: SchemaHead | None = None
    schema_before_sha256: Sha256 | None = None
    schema_after_sha256: Sha256 | None = None
    model_id: UUID | None = None
    model_variant_id: UUID | None = None
    peer_runtime_sha256: Sha256 | None = None
    collective_verified: bool | None = None
    model_manifest_sha256: Sha256 | None = None
    output_sha256: Sha256 | None = None
    backup_validation_sha256: Sha256 | None = None
    check: Literal["node", "gateway", "collective", "source"] | None = None
    generated_tokens: int | None = Field(default=None, ge=0, le=64)
    elapsed_s: float | None = Field(default=None, ge=0, le=900, allow_inf_nan=False)
    process_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    process_created_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    container_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    service_image_ids: dict[
        ServiceName, Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    ] = Field(default_factory=dict, max_length=6)
    source_verified: bool | None = None
    artifacts_verified: bool | None = None
    configuration_verified: bool | None = None
    migration_exit_code: int | None = Field(default=None, ge=0, le=255)
    migration_not_started_sha256: Sha256 | None = None
    cleanup_verified: bool | None = None

    @model_validator(mode="after")
    def complete_process_identity(self) -> UpgradeEvidence:
        if (self.process_id is None) != (self.process_created_at is None):
            raise ValueError("evidence process identity is incomplete")
        return self


class UpgradeReceipt(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    sequence: int = Field(ge=1, le=2**63 - 1)
    effect_id: ArtifactKey
    phase: UpgradePhase
    occurred_at: AwareDatetime
    outcome: Literal["intent", "complete", "failed", "unknown"]
    observed_sha256: Sha256 | None = None
    reason: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    evidence: str = Field(default="", max_length=8192)
    restoration: Literal["not_required", "pending", "verified", "failed"] = "not_required"

    @model_validator(mode="after")
    def bounded_safe_evidence(self) -> UpgradeReceipt:
        if self.evidence:
            UpgradeEvidence.model_validate_json(self.evidence)
        return self


class UpgradeEvent(UpgradeReceipt):
    pass


class UpgradeReceiptBatch(UpgradeWire):
    receipts: tuple[UpgradeReceipt, ...] = Field(min_length=1, max_length=32)


class UpgradeReceiptAck(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    sequence: int = Field(ge=0, le=2**63 - 1)


class UpgradeEventPage(UpgradeWire):
    operation: UpgradeOperation
    events: tuple[UpgradeEvent, ...] = Field(max_length=100)
    cursor: int = Field(ge=0, le=2**63 - 1)
    reset: bool = False


class UpgradeClaimRequest(UpgradeWire):
    executor_id: ArtifactKey
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    observed_source_sha256: Sha256


class UpgradeClaimIdle(UpgradeWire):
    idle: Literal[True] = True


class UpgradeActivationRequest(UpgradeWire):
    generation: int = Field(ge=1, le=2**63 - 1)
    source_sha256: Sha256
    boundary: Literal["migration", "runtime"] = "runtime"


class UpgradeActivationGrant(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    boundary: Literal["migration", "runtime"]
    grant_id: UUID
    expires_at: AwareDatetime


class UpgradeActivationConsume(UpgradeWire):
    grant: UpgradeActivationGrant
    intent: UpgradeReceipt

    @model_validator(mode="after")
    def matching_activation(self) -> UpgradeActivationConsume:
        phase = (
            UpgradePhase.MIGRATING
            if self.grant.boundary == "migration"
            else UpgradePhase.ACTIVATING
        )
        if (
            (self.grant.job_id, self.grant.generation)
            != (self.intent.job_id, self.intent.generation)
            or self.intent.phase is not phase
            or self.intent.outcome != "intent"
            or self.intent.occurred_at >= self.grant.expires_at
        ):
            raise ValueError("activation grant and durable intent differ")
        return self


class NodeUpgradeCommand(UpgradeExecutionPlan):
    """Node accepts the same immutable authority as the Core executor."""

    activation_grant: UpgradeActivationGrant | None = None
    activation_receipt: UpgradeReceipt | None = None
    peer_inventory: NodeUpgradeInventory | None = None

    @model_validator(mode="after")
    def studio_only(self) -> NodeUpgradeCommand:
        if self.operation.kind is not UpgradeKind.STUDIO:
            raise ValueError("node command needs Studio target")
        grant = self.activation_grant
        if (grant is None) != (self.activation_receipt is None):
            raise ValueError("node activation requires the consumed intent receipt")
        if grant is not None and (
            (grant.job_id, grant.generation, grant.boundary)
            != (self.operation.job_id, self.operation.generation, "runtime")
            or grant.expires_at != self.authorization_expires_at
        ):
            raise ValueError("node activation grant differs")
        if grant is not None and self.activation_receipt is not None:
            UpgradeActivationConsume(grant=grant, intent=self.activation_receipt)
            if self.activation_receipt.observed_sha256 != self.operation.source_sha256:
                raise ValueError("node activation source observation differs")
        peer = self.peer_inventory
        if peer is not None and (
            grant is None
            or (peer.job_id, peer.generation, peer.node, peer.runtime_sha256)
            != (
                self.operation.job_id,
                self.operation.generation,
                "coire-edge-b" if self.operation.node == "coire-edge-a" else "coire-edge-a",
                self.peer_runtime_sha256,
            )
            or peer.node not in self.maintenance.targets
        ):
            raise ValueError("node activation peer observation differs")
        if grant is not None and self.peer_runtime_sha256 is not None and peer is None:
            raise ValueError("node activation requires its fenced peer observation")
        return self


class NodeUpgradeCancel(UpgradeWire):
    generation: int = Field(ge=1, le=2**63 - 1)
    reason: str = Field(default="administrator_cancelled", max_length=256)
    unstarted_plan: UpgradeExecutionPlan | None = None

    @model_validator(mode="after")
    def unstarted_authority(self) -> NodeUpgradeCancel:
        plan = self.unstarted_plan
        if plan is not None and (
            plan.operation.kind is not UpgradeKind.STUDIO
            or plan.operation.generation != self.generation
            or plan.operation.phase is not UpgradePhase.ACCEPTED
            or plan.operation.receipt_sequence != 0
            or plan.operation.finished_at is not None
        ):
            raise ValueError("unstarted cancellation requires an untouched accepted Studio plan")
        return self


class NativeUnstartedClosure(UpgradeWire):
    """Durable refusal of delayed preparation after positively absent local effects."""

    node: StudioName
    plan_sha256: Sha256
    receipt: UpgradeReceipt

    @model_validator(mode="after")
    def unchanged_terminal(self) -> NativeUnstartedClosure:
        if (
            self.receipt.phase is not UpgradePhase.CANCELLED_UNCHANGED
            or self.receipt.sequence != 1
            or self.receipt.outcome != "complete"
            or self.receipt.observed_sha256 is None
        ):
            raise ValueError("unstarted closure requires unchanged-source completion")
        return self


class NodeUpgradePeerRead(UpgradeWire):
    """A changed Studio can observe only the peer fenced by its accepted operation."""

    protocol: Literal[1] = 1
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256


class NodeUpgradeCollectiveRead(NodeUpgradePeerRead):
    """Bound collective observation requested by the changed Studio's guard."""

    stage: Literal["candidate", "source"]
    runtime_sha256: Sha256
    deadline_at: AwareDatetime


class NodeUpgradeCollectiveScope(UpgradeWire):
    """Core-authored fixed rank-zero collective within accepted paired maintenance."""

    plan: UpgradeExecutionPlan
    request: NodeUpgradeCollectiveRead
    command_id: UUID
    index: Literal[0, 1, 2]

    @model_validator(mode="after")
    def accepted_pair(self) -> NodeUpgradeCollectiveScope:
        plan = self.plan
        expected = plan.target if self.request.stage == "candidate" else plan.source
        if (
            plan.operation.kind is not UpgradeKind.STUDIO
            or plan.operation.node not in {"coire-edge-a", "coire-edge-b"}
            or plan.peer_runtime_sha256 is None
            or set(plan.maintenance.targets) != {"coire-edge-a", "coire-edge-b"}
            or (self.request.generation, self.request.plan_sha256, self.request.runtime_sha256)
            != (
                plan.operation.generation,
                plan.authority_sha256(),
                expected.manifest.environment_sha256,
            )
            or self.command_id
            != uuid5(
                NAMESPACE_URL,
                "coire-upgrade-collective/"
                + plan.operation.job_id
                + "/"
                + self.request.stage
                + "/"
                + self.request.plan_sha256
                + "/"
                + str(self.index),
            )
        ):
            raise ValueError("collective scope differs from accepted exact pair")
        return self

    def runtime_pair(self) -> tuple[str, str]:
        peer = self.plan.peer_runtime_sha256
        assert peer is not None
        changed = self.request.runtime_sha256
        return (changed, peer) if self.plan.operation.node == "coire-edge-a" else (peer, changed)


class NodeUpgradeCollectiveCleanup(UpgradeWire):
    """Cleanup-only authority: retains the original scope and grants no new probe."""

    scope: NodeUpgradeCollectiveScope
    cleanup_only: Literal[True] = True


class NodeUpgradeCollectiveCleanupReceipt(UpgradeWire):
    """Physical cleanup evidence is explicitly not a measured LinkObservation."""

    job_id: UpgradeId
    generation: int = Field(ge=1)
    command_id: UUID
    scope_sha256: Sha256
    namespace_inventory_sha256s: dict[StudioName, Sha256] = Field(min_length=2, max_length=2)
    observed_at: AwareDatetime
    cleanup_verified: Literal[True] = True
    outcome: Literal["interrupted"] = "interrupted"

    @model_validator(mode="after")
    def exact_namespaces(self) -> NodeUpgradeCollectiveCleanupReceipt:
        if set(self.namespace_inventory_sha256s) != {"coire-edge-a", "coire-edge-b"}:
            raise ValueError("collective cleanup requires both declared namespaces")
        return self


class NodeUpgradeStatus(UpgradeWire):
    node: StudioName
    receipts: tuple[UpgradeReceipt, ...] = Field(default=(), max_length=32)
    release_sha256: Sha256 | None = None
    runtime_sha256: Sha256 | None = None
    environment_id: ArtifactKey | None = None
    source_sha256: Sha256 | None = None
    retained_source_sha256: Sha256 | None = None
    retained_job_id: UpgradeId | None = None
    bootstrap_ready: bool = False
    job_id: UpgradeId | None = None
    generation: int = Field(default=0, ge=0, le=2**63 - 1)
    phase: UpgradePhase | None = None
    receipt_sequence: int = Field(default=0, ge=0, le=2**63 - 1)
    reason: str | None = Field(default=None, max_length=256)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def matching_receipt_page(self) -> NodeUpgradeStatus:
        if (self.retained_source_sha256 is None) != (self.retained_job_id is None):
            raise ValueError("retained native source identity is incomplete")
        if any(
            (receipt.job_id, receipt.generation) != (self.job_id, self.generation)
            or receipt.sequence > self.receipt_sequence
            for receipt in self.receipts
        ):
            raise ValueError("node receipt authority differs")
        if any(
            right.sequence != left.sequence + 1
            for left, right in zip(self.receipts, self.receipts[1:], strict=False)
        ):
            raise ValueError("node receipt page is not contiguous")
        return self


class NodeAgentProcessIdentity(UpgradeWire):
    pid: int = Field(ge=1, le=2**31 - 1)
    create_time_s: float = Field(gt=0, allow_inf_nan=False)
    environment_sha256: Sha256


class NativeGuardStatus(UpgradeWire):
    protocol: Literal[1] = 1
    node: StudioName
    bootstrap_runtime: PreparedNativeRuntime
    process: NodeAgentProcessIdentity
    selected: NativeRuntimeSelection
    ready: bool = False
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def matching_guard_identity(self) -> NativeGuardStatus:
        if (
            self.node != self.selected.node
            or self.process.environment_sha256 != self.bootstrap_runtime.environment_sha256
        ):
            raise ValueError("native guard identity differs")
        return self


class NativeNodeProcess(UpgradeWire):
    protocol: Literal[1] = 1
    selected: NativeRuntimeSelection
    process: NodeAgentProcessIdentity
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def matching_selected_process(self) -> NativeNodeProcess:
        if self.process.environment_sha256 != self.selected.runtime.environment_sha256:
            raise ValueError("native node process differs from selected runtime")
        return self


class NativeBootstrapRefresh(UpgradeWire):
    """Retain the old owner before an idle helper moves to verified current Source."""

    previous_runtime: PreparedNativeRuntime
    selected: NativeRuntimeSelection
    source_process: NodeAgentProcessIdentity
    prepared_at: AwareDatetime
    state: Literal["intent", "committed"] = "intent"

    @model_validator(mode="after")
    def source_bound(self) -> NativeBootstrapRefresh:
        if (
            self.selected.job_id is None
            or self.selected.generation < 1
            or self.previous_runtime == self.selected.runtime
            or self.source_process.environment_sha256 != self.selected.runtime.environment_sha256
        ):
            raise ValueError("native helper refresh lacks distinct verified Source authority")
        return self


class NativeUpgradeClaim(UpgradeWire):
    protocol: Literal[1] = 1
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    node: StudioName
    plan_sha256: Sha256
    state: Literal["active", "released"] = "active"
    updated_at: AwareDatetime


class NativeRecoveryIntent(UpgradeWire):
    protocol: Literal[1] = 1
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    source: NativeRuntimeSelection
    candidate: PreparedNativeRuntime
    source_process: NodeAgentProcessIdentity
    phase: Literal[
        "prepared",
        "activation_intent",
        "candidate_selected",
        "restoring",
        "source_selected",
        "unchanged_verified",
        "candidate_verified",
        "source_verified",
        "committed",
        "recovery_required",
    ] = "prepared"
    activation_started_at: AwareDatetime | None = None
    activation_grant_id: UUID | None = None
    readiness_deadline_at: AwareDatetime | None = None
    recovery_started_at: AwareDatetime | None = None
    recovery_deadline_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def recovery_authority(self) -> NativeRecoveryIntent:
        from datetime import timedelta

        if self.source_process.environment_sha256 != self.source.runtime.environment_sha256:
            raise ValueError("native source process differs from retained source")
        if (self.activation_started_at is None) != (self.readiness_deadline_at is None):
            raise ValueError("native activation deadline is incomplete")
        if (self.activation_started_at is None) != (self.activation_grant_id is None):
            raise ValueError("native activation grant identity is incomplete")
        if (
            self.activation_started_at is not None
            and self.readiness_deadline_at is not None
            and (
                not self.activation_started_at
                < self.readiness_deadline_at
                <= self.activation_started_at + timedelta(seconds=120)
            )
        ):
            raise ValueError("native readiness deadline differs")
        if (self.recovery_started_at is None) != (self.recovery_deadline_at is None):
            raise ValueError("native recovery deadline is incomplete")
        if (
            self.recovery_started_at is not None
            and self.recovery_deadline_at is not None
            and not (
                self.recovery_started_at
                < self.recovery_deadline_at
                <= self.recovery_started_at + timedelta(seconds=300)
            )
        ):
            raise ValueError("native recovery deadline differs")
        if (
            self.phase not in {"prepared", "unchanged_verified", "recovery_required"}
            and self.activation_started_at is None
        ):
            raise ValueError("native effect requires durable activation intent")
        if (
            self.phase in {"restoring", "source_selected", "source_verified"}
            and self.recovery_deadline_at is None
        ):
            raise ValueError("native restoration deadline is absent")
        return self


class UpgradeJournalEntry(UpgradeWire):
    receipt: UpgradeReceipt
    previous_sha256: Sha256 | None = None
    resource_identity: str | None = Field(default=None, max_length=256)


class DeploymentOwnership(UpgradeWire):
    protocol: Literal[1] = 1
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    state: Literal["active", "committed"] = "active"
    owner_kind: Literal["host", "executor"]
    stage: Literal["preparing", "mutating", "selected"] = "mutating"
    owner_identity: ArtifactKey
    executor_runtime_id: ArtifactKey | None = None
    executor_container_id: Sha256 | None = None
    boot_identity: ArtifactKey
    job_id: UpgradeId | None = None
    operation_generation: int | None = Field(default=None, ge=1, le=2**63 - 1)
    generation: int = Field(ge=1, le=2**63 - 1)
    source_sha256: Sha256 | None = None
    committed_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def operation_binding(self) -> DeploymentOwnership:
        if (self.executor_runtime_id is None) != (self.executor_container_id is None) or (
            self.owner_kind != "executor" and self.executor_runtime_id is not None
        ):
            raise ValueError("deployment executor incarnation is incomplete or misplaced")
        if (self.job_id is None) != (self.operation_generation is None):
            raise ValueError("deployment operation fence is incomplete")
        if self.owner_kind == "host" and self.job_id is not None:
            raise ValueError("host deployment cannot claim an executor job")
        return self


class DeploymentLease(UpgradeWire):
    protocol: Literal[1] = 1
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    owner_identity: ArtifactKey
    kernel_platform: Literal["darwin", "linux"]
    kernel_boot_identity: ArtifactKey
    created_at: AwareDatetime


class DeploymentOperationHold(UpgradeWire):
    """Cluster intent prevents host stack/credential mutation during managed maintenance."""

    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    source_sha256: Sha256
    released: bool = False
    created_at: AwareDatetime


class DeploymentSelectionReceipt(UpgradeWire):
    """Source-independent host pointer reconciliation after verified runtime selection."""

    protocol: Literal[1] = 1
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    owner_identity: ArtifactKey
    boot_identity: ArtifactKey
    generation: int = Field(ge=1, le=2**63 - 1)
    release_key: ArtifactKey
    compose_sha256: Sha256
    protected_release_keys: tuple[ArtifactKey, ...] = Field(min_length=1, max_length=512)
    protected_compose_sha256s: dict[ArtifactKey, Sha256] = Field(min_length=1, max_length=512)
    selected_at: AwareDatetime
    managed_release_id: UUID | None = None
    manifest_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def retained_selection(self) -> DeploymentSelectionReceipt:
        if self.release_key not in self.protected_release_keys or len(
            set(self.protected_release_keys)
        ) != len(self.protected_release_keys):
            raise ValueError("selected release retention differs")
        if (
            set(self.protected_compose_sha256s) != set(self.protected_release_keys)
            or self.protected_compose_sha256s[self.release_key] != self.compose_sha256
        ):
            raise ValueError("retained release content binding differs")
        if (self.managed_release_id is None) != (self.manifest_sha256 is None):
            raise ValueError("managed selection identity is incomplete")
        return self


class NativeSmokeCommand(UpgradeWire):
    """Bounded private child input; all local paths resolve from configured roots."""

    protocol: Literal[1] = 1
    plan: UpgradeExecutionPlan
    runtime_sha256: Sha256
    stage: Literal["candidate-smoke", "candidate-serving", "source-serving"] = "candidate-smoke"
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def accepted_runtime(self) -> NativeSmokeCommand:
        expected = self.plan.source if self.stage == "source-serving" else self.plan.target
        if (
            self.plan.operation.kind is not UpgradeKind.STUDIO
            or self.runtime_sha256 != expected.manifest.environment_sha256
        ):
            raise ValueError("native smoke runtime differs from accepted authority")
        return self


class NativeSmokeEngineRecord(UpgradeWire):
    """Strict subset of a normal engine journal permitted for owned smoke cleanup."""

    engine_id: UUID
    target: None = None
    engine_version: str | None = Field(default=None, max_length=128)
    slug: str = Field(pattern=SLUG_PATTERN)
    port: int = Field(ge=1024, le=65535)
    pid: int = Field(ge=1, le=2**31 - 1)
    create_time: float = Field(gt=0, allow_inf_nan=False)
    estimate_bytes: int = Field(ge=1, le=2 * 1024**3)
    backend: Literal["mlx_lm"] = "mlx_lm"
    started_at: AwareDatetime
    chat_template_sha256: None = None
    rendering_identity: None = None
    stderr_file: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}\.log$")


class NativeAbortIntent(UpgradeWire):
    """Capture unchanged-source cleanup once, including before any activation."""

    protocol: Literal[1] = 1
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    reason: Literal["cancelled", "authorization_revoked", "drain_deadline", "candidate_failed"]
    captured_at: AwareDatetime
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def captured_abort_budget(self) -> NativeAbortIntent:
        from datetime import timedelta

        if not self.captured_at < self.deadline_at <= self.captured_at + timedelta(seconds=300):
            raise ValueError("native unchanged cleanup deadline differs")
        return self


NativeSmokePhase = Literal[
    "validating",
    "starting",
    "generating",
    "generation_verified",
    "cleaning",
    "cleanup_verified",
    "verifying_assets",
    "complete",
]


class NativeSmokeProgress(UpgradeWire):
    """Bounded private diagnostic progress; never serving or cleanup authority."""

    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    runtime_sha256: Sha256
    phase: NativeSmokePhase
    observed_at: AwareDatetime
    failure_phase: NativeSmokePhase | None = None
    failure_code: Literal["engine_readiness", "ownership", "verification"] | None = None


class NativeSmokeAttempt(UpgradeWire):
    """Owned child intent survives a lost response or independent guard restart."""

    protocol: Literal[1] = 1
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    stage: Literal["candidate-smoke", "candidate-serving", "source-serving"]
    runtime_sha256: Sha256
    state: Literal["intent", "running", "complete", "failed", "uncertain"] = "intent"
    process: NodeAgentProcessIdentity | None = None
    evidence: UpgradeEvidence | None = None
    cleanup_verified: bool = False
    deadline_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def owned_serving_child(self) -> NativeSmokeAttempt:
        if self.process is not None and self.process.environment_sha256 != self.runtime_sha256:
            raise ValueError("native smoke process runtime differs")
        if self.state == "running" and self.process is None:
            raise ValueError("native smoke running process identity is absent")
        if self.state == "complete" and (
            self.process is None
            or not self.cleanup_verified
            or self.evidence is None
            or self.evidence.cleanup_verified is not True
            or self.evidence.runtime_sha256 != self.runtime_sha256
            or self.evidence.generated_tokens is None
            or self.evidence.generated_tokens < 10
            or self.evidence.output_sha256 is None
        ):
            raise ValueError("native smoke completion lacks owned serving proof")
        if self.state == "failed" and not self.cleanup_verified:
            raise ValueError("native smoke failed cleanup is uncertain")
        return self


class BootstrapSourceObservation(UpgradeWire):
    """Whole prepared Source identity from original bounded physical reads."""

    release_id: UUID
    source_sha256: Sha256
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    container_ids: dict[ServiceName, Sha256] = Field(min_length=1, max_length=6)
    image_ids: dict[ServiceName, Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]] = Field(
        min_length=1, max_length=6
    )
    observed_at: AwareDatetime
    configuration_verified: Literal[True]
    source_healthy: Literal[True]

    @model_validator(mode="after")
    def complete_source_identity(self) -> BootstrapSourceObservation:
        if set(self.container_ids) != set(self.image_ids) or len(
            set(self.container_ids.values())
        ) != len(self.container_ids):
            raise ValueError("bootstrap Source roles or independent containers differ")
        return self


class CoreBootstrapSeed(UpgradeWire):
    """Trusted administrative first-install data, with no application mutation authority."""

    source: UpgradeRelease
    executor_release: UpgradeRelease | None = None
    source_container_ids: dict[ServiceName, Sha256] = Field(min_length=1, max_length=6)
    executor_runtime_id: ArtifactKey
    executor_container_id: Sha256
    executor_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation: int = Field(ge=1, le=2**63 - 1)
    prepared_at: AwareDatetime

    @model_validator(mode="after")
    def independent_source(self) -> CoreBootstrapSeed:
        if (
            self.source.manifest.kind is not UpgradeKind.CONTROL_PLANE
            or set(self.source_container_ids)
            != {item.service for item in self.source.manifest.services}
            or len(set(self.source_container_ids.values())) != len(self.source_container_ids)
            or self.executor_container_id in self.source_container_ids.values()
            or (
                self.executor_release is not None
                and (
                    self.executor_release.manifest.kind is not UpgradeKind.CONTROL_PLANE
                    or self.executor_release.manifest.project != self.source.manifest.project
                )
            )
        ):
            raise ValueError("Core bootstrap requires complete independent Source and executor")
        return self

    @property
    def executor_source(self) -> UpgradeRelease:
        return self.executor_release or self.source


class CoreBootstrapPhysicalObservation(UpgradeWire):
    """Read-only bootstrap proof; a separate held writer is required for readiness."""

    seed_sha256: Sha256
    source: BootstrapSourceObservation
    executor_runtime_id: ArtifactKey
    executor_container_id: Sha256
    executor_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    executor_started_at: AwareDatetime
    observed_at: AwareDatetime
    configuration_verified: Literal[True]

    @model_validator(mode="after")
    def original_observation(self) -> CoreBootstrapPhysicalObservation:
        if (
            self.executor_container_id in self.source.container_ids.values()
            or self.executor_started_at > self.observed_at
            or self.observed_at > self.source.observed_at
        ):
            raise ValueError("Core bootstrap physical observation identity or time differs")
        return self


class CoreBootstrapHeartbeat(UpgradeWire):
    """Qualified physical Source and a held effect-free recovery owner."""

    executor_id: ArtifactKey
    seed: CoreBootstrapSeed
    physical: CoreBootstrapPhysicalObservation
    ownership: DeploymentOwnership
    lease: DeploymentLease
    exclusive_writer_verified: Literal[True]

    @model_validator(mode="after")
    def qualified_owner(self) -> CoreBootstrapHeartbeat:
        seed, proof, owner, lease = self.seed, self.physical, self.ownership, self.lease
        if (
            proof.seed_sha256 != seed.sha256()
            or (proof.source.release_id, proof.source.source_sha256, proof.source.project)
            != (seed.source.release_id, seed.source.manifest_sha256, seed.source.manifest.project)
            or proof.source.container_ids != seed.source_container_ids
            or proof.source.image_ids
            != {role.service: role.image_id for role in seed.source.manifest.services}
            or (proof.executor_runtime_id, proof.executor_container_id, proof.executor_image_id)
            != (seed.executor_runtime_id, seed.executor_container_id, seed.executor_image_id)
            or (
                owner.owner_kind,
                owner.project,
                owner.owner_identity,
                owner.executor_runtime_id,
                owner.executor_container_id,
                owner.state,
                owner.stage,
                owner.job_id,
                owner.source_sha256,
            )
            != (
                "executor",
                seed.source.manifest.project,
                self.executor_id,
                seed.executor_runtime_id,
                seed.executor_container_id,
                "active",
                "preparing",
                None,
                seed.source.manifest_sha256,
            )
            or (lease.project, lease.owner_identity, lease.kernel_boot_identity)
            != (owner.project, owner.owner_identity, owner.boot_identity)
        ):
            raise ValueError("Core bootstrap heartbeat differs from qualified Source ownership")
        return self


class BootstrapStabilityObservation(UpgradeWire):
    """Original physical reads of both retained executor owners during handover."""

    generation: int = Field(ge=1, le=2**63 - 1)
    incumbent_id: ArtifactKey
    candidate_id: ArtifactKey
    incumbent_container_id: Sha256
    candidate_container_id: Sha256
    incumbent_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    candidate_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    kernel_boot_identity: ArtifactKey
    candidate_started_at: AwareDatetime
    observed_at: AwareDatetime
    incumbent_healthy: Literal[True]
    candidate_healthy: Literal[True]
    exclusive_writer_verified: Literal[True]

    @model_validator(mode="after")
    def independent_physical_owners(self) -> BootstrapStabilityObservation:
        if (
            self.incumbent_id == self.candidate_id
            or self.incumbent_container_id == self.candidate_container_id
            or self.candidate_started_at > self.observed_at
        ):
            raise ValueError("executor stability requires independent physical owners")
        return self


class BootstrapHandover(UpgradeWire):
    protocol: Literal[1] = 1
    incumbent_id: ArtifactKey
    candidate_id: ArtifactKey
    incumbent_container_id: Sha256
    candidate_container_id: Sha256
    incumbent_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    candidate_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation: int = Field(ge=1, le=2**63 - 1)
    phase: Literal["passive", "verified", "claimed", "stable", "committed", "restoring", "restored"]
    deadline_at: AwareDatetime
    updated_at: AwareDatetime
    stability_started_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def independent_supervisor(self) -> BootstrapHandover:
        from datetime import timedelta

        if (
            self.incumbent_id == self.candidate_id
            or self.incumbent_container_id == self.candidate_container_id
        ):
            raise ValueError("executor handover requires an independent incumbent")
        if self.phase in {"claimed", "stable", "committed"} and (
            self.stability_started_at is None
            or self.deadline_at != self.stability_started_at + timedelta(seconds=120)
        ):
            raise ValueError("executor stability window is absent or differs")
        return self


class UpgradeComposeHealth(UpgradeWire):
    test: tuple[str, ...] = Field(min_length=2, max_length=32)
    interval: str = "20s"
    timeout: str = "2s"
    retries: int = Field(default=3, ge=1, le=10)
    start_period: str = "10s"
    start_interval: str | None = None


class UpgradeComposeConfiguration(UpgradeWire):
    """The supported resolved Compose subset; unknown settings block replacement."""

    image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    read_only: Literal[True]
    cap_drop: tuple[Literal["ALL"], ...] = Field(min_length=1, max_length=1)
    security_opt: tuple[Literal["no-new-privileges:true"], ...] = Field(min_length=1, max_length=1)
    restart: Literal["no", "always", "unless-stopped", "on-failure"]
    stop_grace_period: str = "5s"
    stop_signal: str | None = None
    pids_limit: int = Field(ge=1, le=1024)
    mem_limit: str | int | None = None
    cpus: str | float | int | None = None
    deploy: dict[str, JsonValue] | None = None
    tmpfs: tuple[str, ...] = Field(default=(), max_length=8)
    networks: dict[str, dict[str, JsonValue] | None] = Field(min_length=1, max_length=8)
    ports: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=16)
    volumes: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=32)
    secrets: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=16)
    environment: dict[str, str | None] = Field(default_factory=dict, max_length=512)
    entrypoint: tuple[str, ...] | None = Field(default=None, max_length=32)
    command: tuple[str, ...] | None = Field(default=None, max_length=32)
    working_dir: str | None = Field(default=None, max_length=4096)
    logging: dict[str, JsonValue]
    depends_on: dict[str, dict[str, JsonValue]] = Field(default_factory=dict, max_length=32)
    labels: dict[str, str] = Field(default_factory=dict, max_length=64)
    profiles: tuple[str, ...] = Field(default=(), max_length=8)
    expose: tuple[str, ...] = Field(default=(), max_length=16)
    init: Literal[False] = False


class UpgradeComposeExecution(UpgradeComposeConfiguration):
    """Mutable first-party roles require the existing explicit non-root identity."""

    user: str = Field(pattern=r"^[1-9][0-9]{0,9}:[1-9][0-9]{0,9}$")


class UpgradeComposeService(UpgradeComposeExecution):
    """Long-running cohort roles always require an explicit healthcheck."""

    healthcheck: UpgradeComposeHealth


class UpgradeComposeDatabase(UpgradeComposeConfiguration):
    """Read-only verification of the existing PostgreSQL exception; never a create role."""

    user: Literal["0", "0:0", "999:999"] | None = None
    stop_grace_period: str = "10s"
    healthcheck: UpgradeComposeHealth
    tmpfs: tuple[Literal["/tmp", "/run/postgresql", "/var/run/postgresql"], ...] = Field(
        default=("/tmp", "/run/postgresql", "/var/run/postgresql"), min_length=3, max_length=3
    )
    cap_add: tuple[Literal["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"], ...] = Field(
        min_length=5, max_length=5
    )
    ports: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=0)
    expose: tuple[str, ...] = Field(default=(), max_length=0)
    entrypoint: None = None
    command: None = None

    @model_validator(mode="after")
    def fixed_database_exception(self) -> UpgradeComposeDatabase:
        if set(self.cap_add) != {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}:
            raise ValueError("database capabilities differ from the existing PostgreSQL exception")
        if self.healthcheck.test != ("CMD-SHELL", "pg_isready -U coire -d coire"):
            raise ValueError("database readiness command differs")
        if len(set(self.tmpfs)) != 3 or self.depends_on:
            raise ValueError("database temporary mounts or dependencies differ")
        return self


class CoreDependencyObservation(UpgradeWire):
    """Exact fresh dependency metadata carries no engine output or credentials."""

    role: Literal["postgres", "coire-api", "coire-migrate"]
    container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,255}$")
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    observed_at: AwareDatetime
    state: Literal["healthy", "completed"]
    configuration_sha256: Sha256

    @model_validator(mode="after")
    def role_state(self) -> CoreDependencyObservation:
        if (self.role == "coire-migrate") != (self.state == "completed"):
            raise ValueError("dependency readiness differs from its fixed role")
        return self


class UpgradeComposeMigration(UpgradeComposeExecution):
    """A fixed one-shot migration has terminal exit evidence instead of readiness."""

    restart: Literal["no"]
    ports: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=0)
    expose: tuple[str, ...] = Field(default=(), max_length=0)
    entrypoint: None = None
    command: None = None

    @model_validator(mode="after")
    def fixed_migration_networks(self) -> UpgradeComposeMigration:
        if set(self.networks) != {"coire-db", "coire-telemetry"}:
            raise ValueError("migration requires its existing database and telemetry networks")
        return self


class UpgradeMountBinding(UpgradeWire):
    """Observed existing source mount; never permission to create a volume or directory."""

    type: Literal["bind", "volume"]
    source: str = Field(min_length=1, max_length=4096)
    target: str = Field(min_length=1, max_length=4096, pattern=r"^/")
    read_only: bool
    logical_source: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def bounded_paths(self) -> UpgradeMountBinding:
        for value in (self.source, self.target, self.logical_source):
            if "\x00" in value or ".." in value.split("/"):
                raise ValueError("mount identity contains an invalid path")
        if self.type == "bind" and not self.source.startswith("/"):
            raise ValueError("bind source must be absolute")
        if self.type == "volume" and not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,255}", self.source
        ):
            raise ValueError("volume identity is invalid")
        return self


class UpgradeDockerExecution(UpgradeWire):
    container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    name: str = Field(min_length=1, max_length=256)
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    observed_at: AwareDatetime
    running: bool
    health: Literal["healthy", "unhealthy", "starting", "none"]
    exit_code: int
    mounts: tuple[UpgradeMountBinding, ...] = Field(default=(), max_length=48)
    network_ids: dict[str, Sha256] = Field(min_length=1, max_length=8)


class UpgradeDockerContainer(UpgradeDockerExecution):
    service: ServiceName


class UpgradeDockerMigration(UpgradeDockerExecution):
    service: Literal["coire-migrate"] = "coire-migrate"
    health: Literal["none"] = "none"
    status: Literal["created", "running", "exited", "dead"]


class CoreSelectedMigration(UpgradeWire):
    """Retain the last completed migration even across later binary-only releases."""

    container: UpgradeDockerMigration
    release: UpgradeRelease
    configuration: UpgradeComposeMigration
    origin_job_id: UpgradeId | None = None
    origin_generation: int | None = Field(default=None, ge=1, le=2**63 - 1)
    origin_plan_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def completed_origin(self) -> CoreSelectedMigration:
        origin = (self.origin_job_id, self.origin_generation, self.origin_plan_sha256)
        if any(item is None for item in origin) and any(item is not None for item in origin):
            raise ValueError("selected migration origin is incomplete")
        name = (
            f"{self.container.project}-coire-migrate-1"
            if self.origin_job_id is None
            else f"{self.container.project}-upgrade-{self.origin_job_id.lower()}-coire-migrate"
        )
        if (
            self.release.manifest.kind is not UpgradeKind.CONTROL_PLANE
            or self.release.manifest.project != self.container.project
            or self.release.manifest.host_release_key is None
            or self.release.manifest.compose_sha256 is None
            or self.container.name != name
            or self.container.image_id != self.configuration.image
            or self.container.running
            or self.container.status != "exited"
            or self.container.exit_code != 0
        ):
            raise ValueError("selected migration lacks exact completed release evidence")
        return self


class CoreSelectedService(UpgradeWire):
    container: UpgradeDockerContainer
    origin_job_id: UpgradeId | None = None
    origin_generation: int | None = Field(default=None, ge=1, le=2**63 - 1)
    origin_plan_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def fixed_container_origin(self) -> CoreSelectedService:
        origin = (self.origin_job_id, self.origin_generation, self.origin_plan_sha256)
        if any(item is None for item in origin) and any(item is not None for item in origin):
            raise ValueError("selected Core container origin is incomplete")
        expected = (
            f"{self.container.project}-{self.container.service}-1"
            if self.origin_job_id is None
            else f"{self.container.project}-upgrade-{self.origin_job_id.lower()}-{self.container.service}"
        )
        if self.container.name != expected:
            raise ValueError("selected Core container name differs from its fixed origin")
        return self


class CoreRuntimeSelection(UpgradeWire):
    """Source-independent identity of the currently selected whole service cohort."""

    protocol: Literal[1] = 1
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    release_id: UUID
    manifest_sha256: Sha256
    compose_sha256: Sha256
    host_release_key: ArtifactKey
    owner_identity: ArtifactKey
    generation: int = Field(ge=1, le=2**63 - 1)
    selected_at: AwareDatetime
    services: tuple[CoreSelectedService, ...] = Field(min_length=1, max_length=6)
    migration: CoreSelectedMigration | None = None

    @model_validator(mode="after")
    def complete_selected_roles(self) -> CoreRuntimeSelection:
        roles = [item.container.service for item in self.services]
        containers = [item.container.container_id for item in self.services]
        if len(set(roles)) != len(roles) or len(set(containers)) != len(containers):
            raise ValueError("selected Core roles or containers are duplicated")
        if any(
            item.container.project != self.project
            or not item.container.running
            or item.container.health != "healthy"
            or item.container.observed_at > self.selected_at
            for item in self.services
        ):
            raise ValueError("selected Core runtime lacks healthy exact-project observations")
        if self.migration is not None and (
            self.migration.container.project != self.project
            or self.migration.container.observed_at > self.selected_at
        ):
            raise ValueError("selected migration project or observation time differs")
        return self


class CoreSourceSnapshot(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    source_sha256: Sha256
    captured_at: AwareDatetime
    services: tuple[UpgradeDockerContainer, ...] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def unique_healthy_sources(self) -> CoreSourceSnapshot:
        if len({item.service for item in self.services}) != len(self.services) or len(
            {item.container_id for item in self.services}
        ) != len(self.services):
            raise ValueError("Source snapshot has duplicate roles or containers")
        if any(
            item.project != self.project or not item.running or item.health != "healthy"
            for item in self.services
        ):
            raise ValueError("Source snapshot requires the exact healthy project cohort")
        return self


class UpgradeComposeResources(UpgradeWire):
    """Physical names from the hash-verified frozen top-level Compose document."""

    volumes: dict[str, str] = Field(default_factory=dict, max_length=64)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=64)


class CoreServiceWindow(UpgradeWire):
    service: ServiceName
    captured_at: AwareDatetime
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def bounded_service_window(self) -> CoreServiceWindow:
        if not 0 < (self.deadline_at - self.captured_at).total_seconds() <= 120:
            raise ValueError("Core service readiness window differs")
        return self


class CoreExecutorEpoch(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    activations: tuple[UpgradeActivationConsume, ...] = Field(default=(), max_length=2)
    readiness_deadline: AwareDatetime | None = None
    service_windows: tuple[CoreServiceWindow, ...] = Field(default=(), max_length=6)
    outage_captured_at: AwareDatetime | None = None
    outage_deadline: AwareDatetime | None = None
    recovery_captured_at: AwareDatetime | None = None
    recovery_deadline: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent_epoch(self) -> CoreExecutorEpoch:
        boundaries = [item.grant.boundary for item in self.activations]
        if len({item.service for item in self.service_windows}) != len(self.service_windows) or (
            self.service_windows and "runtime" not in boundaries
        ):
            raise ValueError("Core service readiness windows lack unique runtime authority")
        if len(set(boundaries)) != len(boundaries) or any(
            (item.grant.job_id, item.grant.generation) != (self.job_id, self.generation)
            for item in self.activations
        ):
            raise ValueError("executor activation epoch differs")
        for capture, deadline in (
            (self.outage_captured_at, self.outage_deadline),
            (self.recovery_captured_at, self.recovery_deadline),
        ):
            if (capture is None) != (deadline is None) or (
                capture is not None
                and deadline is not None
                and not 0 < (deadline - capture).total_seconds() <= 300
            ):
                raise ValueError("executor deadline differs")
        if ("runtime" in boundaries) != (self.readiness_deadline is not None):
            raise ValueError("runtime activation requires a captured readiness deadline")
        return self


class CoreExecutorEffect(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    effect_id: ArtifactKey
    action: Literal[
        "migration",
        "create_migration",
        "start_migration",
        "stop_source",
        "create_candidate",
        "start_candidate",
        "stop_candidate",
        "remove_candidate",
        "remove_migration",
        "start_source",
    ]
    service: ServiceName | None = None
    container_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    captured_at: AwareDatetime
    deadline: AwareDatetime
    outcome: Literal["intent", "complete", "unknown"] = "intent"
    observed_container_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def scoped_effect(self) -> CoreExecutorEffect:
        if (
            self.action in {"migration", "create_migration", "start_migration", "remove_migration"}
        ) != (self.service is None):
            raise ValueError("executor effect role differs")
        if self.deadline <= self.captured_at:
            raise ValueError("executor effect deadline differs")
        if (
            self.action
            in {
                "stop_source",
                "start_candidate",
                "stop_candidate",
                "remove_candidate",
                "remove_migration",
                "start_source",
                "start_migration",
            }
            and self.container_id is None
        ):
            raise ValueError("executor lifecycle effect needs an exact container identity")
        return self


class UpgradeMigrationNotStarted(UpgradeWire):
    """An authoritative execution fence, distinct from a completed migration."""

    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    grant_id: UUID
    state: Literal["not_started"] = "not_started"
    schema_head: SchemaHead
    schema_sha256: Sha256
    observed_at: AwareDatetime
    execution_fenced: Literal[True] = True


class UpgradeMigrationExecution(UpgradeWire):
    """A consumed migration attempt is never permission to run the schema change twice."""

    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    grant_id: UUID
    state: Literal["intent", "complete", "uncertain"]
    captured_at: AwareDatetime
    deadline_at: AwareDatetime
    schema_before_head: SchemaHead
    schema_before_sha256: Sha256
    expected_schema_head: SchemaHead
    schema_after_head: SchemaHead | None = None
    schema_after_sha256: Sha256 | None = None
    exit_code: int | None = Field(default=None, ge=0, le=255)
    completed_at: AwareDatetime | None = None

    @property
    def failed_unchanged(self) -> bool:
        """A nonzero terminal attempt has positively preserved its starting structure."""
        return (
            self.state == "uncertain"
            and self.exit_code is not None
            and self.exit_code != 0
            and self.completed_at is not None
            and self.schema_after_head == self.schema_before_head
            and self.schema_after_sha256 == self.schema_before_sha256
        )

    @model_validator(mode="after")
    def terminal_schema_proof(self) -> UpgradeMigrationExecution:
        if not 0 < (self.deadline_at - self.captured_at).total_seconds() <= 120:
            raise ValueError("migration deadline exceeds its captured readiness budget")
        if self.state == "intent":
            if any(
                value is not None
                for value in (
                    self.schema_after_head,
                    self.schema_after_sha256,
                    self.exit_code,
                    self.completed_at,
                )
            ):
                raise ValueError("migration intent cannot invent terminal evidence")
        elif self.completed_at is None or self.completed_at < self.captured_at:
            raise ValueError("migration terminal observation time is absent or invalid")
        if self.state == "complete" and (
            self.exit_code != 0
            or self.schema_after_head != self.expected_schema_head
            or self.schema_after_sha256 is None
            or self.completed_at is None
            or self.completed_at > self.deadline_at
        ):
            raise ValueError("migration completion requires timely zero-exit expected-schema proof")
        return self


class UpgradeDockerImageDefaults(UpgradeWire):
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    environment: tuple[str, ...] = Field(default=(), max_length=512)
    entrypoint: tuple[str, ...] | None = Field(default=None, max_length=32)
    command: tuple[str, ...] | None = Field(default=None, max_length=32)
    working_dir: str = Field(default="", max_length=4096)
    stop_signal: str | None = Field(default=None, max_length=32)
    exposed_ports: tuple[str, ...] = Field(default=(), max_length=64)


class UpgradePublicationArtifact(UpgradeWire):
    key: ArtifactKey
    content_base64: str = Field(max_length=24 * 1024 * 1024)


class UpgradePublicationBundle(UpgradeWire):
    release: UpgradeRelease
    artifacts: tuple[UpgradePublicationArtifact, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_artifacts(self) -> UpgradePublicationBundle:
        if len({item.key for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("publication artifact keys are duplicated")
        if {item.key for item in self.artifacts} != set(self.release.manifest.artifact_sha256s):
            raise ValueError("publication artifact set differs")
        return self


class ExecutorLiveness(UpgradeWire):
    """Authenticated process liveness; deliberately carries no recovery readiness."""

    service: Literal["coire-upgrade-executor"] = "coire-upgrade-executor"
    version: str = Field(min_length=1, max_length=64)
    live: Literal[True] = True


class CoreHandoverPreparation(UpgradeWire):
    """Fixed executor bootstrap preparation; carries no application upgrade authority."""

    protocol: Literal[1] = 1
    incumbent: CoreBootstrapSeed
    candidate_release: UpgradeRelease
    candidate_runtime_id: ArtifactKey
    candidate_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation: int = Field(ge=1, le=2**63 - 1)
    initiator_user_id: UUID
    idempotency_key: UUID
    prepared_at: AwareDatetime
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def independent_candidate(self) -> CoreHandoverPreparation:
        from datetime import timedelta

        if (
            3 not in self.incumbent.executor_source.manifest.compatibility.native_journal_versions
            or 3 not in self.candidate_release.manifest.compatibility.native_journal_versions
            or self.candidate_release.manifest.kind is not UpgradeKind.CONTROL_PLANE
            or self.candidate_release.manifest.project != self.incumbent.source.manifest.project
            or self.candidate_runtime_id == self.incumbent.executor_runtime_id
            or self.candidate_image_id == self.incumbent.executor_image_id
            or self.generation <= self.incumbent.generation
            or self.deadline_at != self.prepared_at + timedelta(seconds=120)
        ):
            raise ValueError("Executor handover preparation identity or deadline differs")
        return self

    @property
    def candidate_name(self) -> str:
        return f"{self.incumbent.source.manifest.project}-coire-upgrade-executor-{self.generation}"


class CoreHandoverUnchangedIntent(UpgradeWire):
    """Durably prohibit create/start before recording an effect-free failure."""

    action: Literal["fail_unchanged"] = "fail_unchanged"
    preparation_sha256: Sha256
    incumbent_seed_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    intent_at: AwareDatetime


class CoreHandoverUnchangedRequest(UpgradeWire):
    preparation_sha256: Sha256
    incumbent: CoreBootstrapHeartbeat


class CoreHandoverCreationRecoveryIntent(UpgradeWire):
    """Capture first creation failure before a full candidate ID is known."""

    preparation_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    detected_at: AwareDatetime
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def original_recovery_window(self) -> CoreHandoverCreationRecoveryIntent:
        if (self.deadline_at - self.detected_at).total_seconds() != 300:
            raise ValueError("Creation recovery must retain its original five-minute budget")
        return self


class CoreHandoverCreation(UpgradeWire):
    """Fsynced before Docker creation; a lost response is reconciled by exact fixed name."""

    preparation_sha256: Sha256
    candidate_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,127}$")
    candidate_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation: int = Field(ge=1, le=2**63 - 1)
    outcome: Literal["intent", "dispatched", "observed"] = "intent"
    candidate_container_id: Sha256 | None = None
    observed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def observed_identity(self) -> CoreHandoverCreation:
        if (self.outcome == "observed") != (
            self.candidate_container_id is not None and self.observed_at is not None
        ) or (
            self.outcome != "observed"
            and (self.candidate_container_id is not None or self.observed_at is not None)
        ):
            raise ValueError("Executor creation observation is partial or differs")
        return self


class ExecutorPassiveReadiness(UpgradeWire):
    """Fresh candidate protocol proof while the incumbent still holds the writer."""

    protocol: Literal[1] = 1
    preparation_sha256: Sha256
    candidate_seed_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    candidate_runtime_id: ArtifactKey
    candidate_container_id: Sha256
    candidate_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    incumbent_runtime_id: ArtifactKey
    incumbent_container_id: Sha256
    observed_at: AwareDatetime
    recovery_ready: Literal[False] = False
    exclusive_writer_held: Literal[False] = False

    @model_validator(mode="after")
    def independent_candidate(self) -> ExecutorPassiveReadiness:
        if (
            self.candidate_container_id == self.incumbent_container_id
            or self.candidate_runtime_id == self.incumbent_runtime_id
        ):
            raise ValueError("Passive candidate must retain an independent incumbent")
        return self


class CoreHandoverStartRequest(UpgradeWire):
    """Admin chooses registered content; runtime/container paths remain server owned."""

    release_id: UUID
    expected_incumbent_seed_sha256: Sha256
    idempotency_key: UUID


class CoreHandoverSummary(UpgradeWire):
    handover_id: UUID
    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    generation: int = Field(ge=1, le=2**63 - 1)
    phase: Literal[
        "accepted",
        "passive",
        "verified",
        "transfer_consumed",
        "claimed",
        "stable",
        "committing",
        "committed",
        "restoring",
        "restored",
        "failed_unchanged",
    ]
    preparation_sha256: Sha256
    candidate_release_id: UUID
    incumbent_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    candidate_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    prepared_at: AwareDatetime
    deadline_at: AwareDatetime


class DeploymentHandoverHold(UpgradeWire):
    """Durable exclusion spans the gap between retained executor kernel writers."""

    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    preparation_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    incumbent_runtime_id: ArtifactKey
    incumbent_container_id: Sha256
    candidate_runtime_id: ArtifactKey
    updated_at: AwareDatetime
    released: bool = False

    @model_validator(mode="after")
    def independent_candidate(self) -> DeploymentHandoverHold:
        if self.incumbent_runtime_id == self.candidate_runtime_id:
            raise ValueError("Executor handover hold requires independent runtimes")
        return self


class CoreHandoverCandidateStart(UpgradeWire):
    preparation_sha256: Sha256
    candidate_seed_sha256: Sha256
    candidate_container_id: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    deadline_at: AwareDatetime
    outcome: Literal["intent", "dispatched", "observed"] = "intent"
    started_at: AwareDatetime | None = None
    observed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def observed_process(self) -> CoreHandoverCandidateStart:
        if self.outcome == "observed":
            if (
                self.started_at is None
                or self.observed_at is None
                or self.started_at > self.observed_at
            ):
                raise ValueError("Candidate start observation is absent or predates process birth")
        elif self.started_at is not None or self.observed_at is not None:
            raise ValueError("Unobserved candidate start cannot carry process evidence")
        return self


class CoreHandoverTransferRequest(UpgradeWire):
    preparation_sha256: Sha256
    passive_proof_sha256: Sha256


class CoreHandoverTransferIntent(UpgradeWire):
    action: Literal["writer_transfer"] = "writer_transfer"
    handover_id: UUID
    intent_id: UUID
    preparation_sha256: Sha256
    candidate_seed_sha256: Sha256
    passive_proof_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    consumed_at: AwareDatetime
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def bounded_transfer(self) -> CoreHandoverTransferIntent:
        if not 0 < (self.deadline_at - self.consumed_at).total_seconds() <= 30:
            raise ValueError("Executor transfer intent exceeds its captured 30-second window")
        return self


class CoreHandoverIncumbentObservation(UpgradeWire):
    preparation_sha256: Sha256
    incumbent_runtime_id: ArtifactKey
    incumbent_container_id: Sha256
    incumbent_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    incumbent_started_at: AwareDatetime
    observed_at: AwareDatetime
    incumbent_healthy: Literal[True]


class CoreHandoverRecoveryIntent(UpgradeWire):
    """Independent Source recovery, bounded from first failure rather than a retry."""

    action: Literal["restore_incumbent"] = "restore_incumbent"
    preparation_sha256: Sha256
    candidate_seed_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    incumbent_container_id: Sha256
    candidate_container_id: Sha256
    detected_at: AwareDatetime
    deadline_at: AwareDatetime
    outcome: Literal["intent", "dispatched", "stopped"] = "intent"
    stopped_observed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def bounded_recovery(self) -> CoreHandoverRecoveryIntent:
        if (self.deadline_at - self.detected_at).total_seconds() != 300:
            raise ValueError("Executor recovery must retain its original five-minute window")
        if self.incumbent_container_id == self.candidate_container_id:
            raise ValueError("Executor recovery cannot stop its independent incumbent")
        if (self.outcome == "stopped") != (self.stopped_observed_at is not None):
            raise ValueError("Executor recovery stop needs positive physical evidence")
        if self.stopped_observed_at is not None and self.stopped_observed_at < self.detected_at:
            raise ValueError("Executor recovery stop observation predates failure detection")
        return self


class CoreHandoverRecoveryRequest(UpgradeWire):
    """Serialize restoration against final SQL commit before any candidate stop."""

    recovery: CoreHandoverRecoveryIntent


class CoreHandoverRetirementIntent(UpgradeWire):
    """One fixed predecessor stop; uncertain dispatch never authorizes a retry."""

    action: Literal["stop_incumbent"] = "stop_incumbent"
    preparation_sha256: Sha256
    candidate_seed_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    incumbent_container_id: Sha256
    candidate_container_id: Sha256
    intent_at: AwareDatetime
    outcome: Literal["intent", "dispatched", "stopped"] = "intent"
    stopped_observed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def independent_predecessor(self) -> CoreHandoverRetirementIntent:
        if self.incumbent_container_id == self.candidate_container_id:
            raise ValueError("Retirement cannot stop the selected candidate")
        if (self.outcome == "stopped") != (self.stopped_observed_at is not None):
            raise ValueError("Retirement needs positive original stop evidence")
        if self.stopped_observed_at is not None and self.stopped_observed_at < self.intent_at:
            raise ValueError("Retirement stop predates its original intent")
        return self


class CoreHandoverStoppedObservation(UpgradeWire):
    preparation_sha256: Sha256
    candidate_seed_sha256: Sha256
    candidate_container_id: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    observed_at: AwareDatetime
    running: Literal[False] = False


class CoreHandoverRestoreRequest(UpgradeWire):
    preparation_sha256: Sha256
    incumbent: CoreBootstrapHeartbeat
    candidate: CoreHandoverStoppedObservation

    @model_validator(mode="after")
    def same_recovery(self) -> CoreHandoverRestoreRequest:
        if self.preparation_sha256 != self.candidate.preparation_sha256:
            raise ValueError("Executor restoration mixes different handover preparations")
        if self.incumbent.seed.executor_container_id == self.candidate.candidate_container_id:
            raise ValueError("Executor restoration cannot stop its retained incumbent")
        return self


class CoreHandoverCommitRequest(UpgradeWire):
    preparation_sha256: Sha256
    stability: BootstrapStabilityObservation


class CoreHandoverCommitIntent(UpgradeWire):
    action: Literal["select_candidate_executor"] = "select_candidate_executor"
    handover_id: UUID
    decision_id: UUID
    preparation_sha256: Sha256
    candidate_seed_sha256: Sha256
    stability_sha256: Sha256
    generation: int = Field(ge=1, le=2**63 - 1)
    decided_at: AwareDatetime


class CoreHandoverCommittedRequest(UpgradeWire):
    preparation_sha256: Sha256
    candidate: CoreBootstrapHeartbeat
    incumbent: CoreHandoverIncumbentObservation

    @model_validator(mode="after")
    def retained_independent_source(self) -> CoreHandoverCommittedRequest:
        if self.preparation_sha256 != self.incumbent.preparation_sha256:
            raise ValueError("Executor commit acknowledgment mixes preparations")
        if self.candidate.seed.executor_container_id == self.incumbent.incumbent_container_id:
            raise ValueError("Executor commit acknowledgment requires an independent incumbent")
        return self
