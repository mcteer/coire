"""Strict contracts for the stateless control-plane failover tier."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from coire_core.failover_crypto import verify_ed25519
from coire_core.models.adapters import InferenceTarget
from coire_core.models.gateway import ChatCompletionRequest

FAILOVER_MEMBER_NAMES = frozenset({"coire-core", "coire-edge-a", "coire-edge-b"})
FAILOVER_MAX_AUTHORITY_S = 300


class FailoverWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal[2] = 2


class FailoverLimits(FailoverWireModel):
    prompt_tokens: int = Field(default=4096, ge=1, le=4096)
    output_tokens: int = Field(default=1024, ge=1, le=1024)
    requests_per_minute: int = Field(default=10, ge=1, le=10)
    concurrency: int = Field(default=1, ge=1, le=1)


class FailoverAccessSubject(FailoverWireModel):
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=256)
    eligible: bool
    mutable_budget_required: bool = False
    policy_version: int = Field(ge=1)
    limits: FailoverLimits = Field(default_factory=FailoverLimits)

    @model_validator(mode="after")
    def budget_is_available(self) -> Self:
        if self.eligible and self.mutable_budget_required:
            raise ValueError("mutable budget accounting is unavailable during failover")
        return self


class FailoverAcknowledgement(FailoverWireModel):
    request_id: UUID
    host: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    epoch: int = Field(ge=1)
    accepted: bool


class FailoverEventAcknowledgement(FailoverWireModel):
    batch_id: UUID
    signature: str = Field(min_length=1)
    host: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    committed_event_ids: list[UUID] = Field(max_length=256)

    @model_validator(mode="after")
    def unique_receipts(self) -> Self:
        if len(set(self.committed_event_ids)) != len(self.committed_event_ids):
            raise ValueError("event acknowledgement IDs must be unique")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class FailoverOverrideAcknowledgement(FailoverAcknowledgement):
    signature: str = Field(min_length=1)

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class HandbackAcknowledgement(FailoverAcknowledgement):
    term: int = Field(ge=1)
    successor: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    fenced: bool
    expires_at: AwareDatetime
    signature: str = Field(min_length=1)

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class FailoverProblem(FailoverWireModel):
    type: str = "about:blank"
    title: str = "Failover request refused"
    status: int = Field(ge=400, le=599)
    reason: Literal[
        "identity_invalid",
        "snapshot_unavailable",
        "not_elected",
        "model_not_resident",
        "unsupported_capability",
        "adapter_unavailable",
        "exact_target_unavailable",
        "maintenance_active",
        "audit_unavailable",
        "request_limit_exceeded",
    ]
    detail: str = Field(max_length=256)


class FailoverMonitorStatus(FailoverWireModel):
    ready: bool


def _canonical_json(value: object) -> bytes:
    """Encode signed material without whitespace or unstable key ordering."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


class FailoverTier(StrEnum):
    FULL = "full"
    DEGRADED_INFERENCE = "degraded_inference"
    MINIMAL = "minimal"


class FailoverMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    priority: int = Field(ge=0, le=2)
    public_key: str = Field(min_length=1)


class FailoverMembershipConfig(FailoverWireModel):
    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    members: list[FailoverMember] = Field(min_length=3, max_length=3)
    signing_key_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def fixed_three_member_cluster(self) -> Self:
        if {member.name for member in self.members} != FAILOVER_MEMBER_NAMES:
            raise ValueError("membership must contain core, edge-a, and edge-b exactly once")
        priorities = {"coire-core": 0, "coire-edge-a": 1, "coire-edge-b": 2}
        if any(member.priority != priorities[member.name] for member in self.members):
            raise ValueError("member priorities must be core, edge-a, edge-b")
        return self


class FailoverHeartbeat(FailoverWireModel):
    """A signed liveness beat. Latency of the exchange is measured by the caller."""

    model_config = ConfigDict(extra="forbid")

    member: str
    epoch: int = Field(default=1, ge=1)
    challenge: UUID = Field(default_factory=uuid4)
    recovery_ready: bool = False
    sent_at: AwareDatetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def member_is_known(self) -> Self:
        if self.member not in FAILOVER_MEMBER_NAMES:
            raise ValueError("heartbeat member must be a configured failover member")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class ElectionVoteRequest(FailoverWireModel):
    """A candidate asking one peer for its single grant in a term."""

    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    term: int = Field(ge=1)
    candidate: str
    request_id: UUID = Field(default_factory=uuid4)
    issued_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def candidate_is_known(self) -> Self:
        if self.candidate not in FAILOVER_MEMBER_NAMES:
            raise ValueError("candidate must be a configured failover member")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class ElectionVoteGrant(FailoverWireModel):
    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    term: int = Field(ge=1)
    candidate: str
    voter: str
    request_id: UUID = Field(default_factory=uuid4)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def member_names_are_known(self) -> Self:
        if self.candidate not in FAILOVER_MEMBER_NAMES or self.voter not in FAILOVER_MEMBER_NAMES:
            raise ValueError("candidate and voter must be configured failover members")
        lifetime = (self.expires_at - self.issued_at).total_seconds()
        if not 0 < lifetime <= FAILOVER_MAX_AUTHORITY_S:
            raise ValueError("grant lifetime must be positive and bounded")
        return self

    def is_current(self, now: datetime | None = None) -> bool:
        """Whether the grant can contribute to a lease at ``now``."""
        now = now or datetime.now(UTC)
        return (
            self.issued_at <= now < self.expires_at
            and (self.expires_at - self.issued_at).total_seconds() <= FAILOVER_MAX_AUTHORITY_S
        )

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class PromotionProof(FailoverWireModel):
    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    term: int = Field(ge=1)
    candidate: str
    grants: list[ElectionVoteGrant] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def coherent_distinct_current_grants(self) -> Self:
        if self.candidate not in FAILOVER_MEMBER_NAMES:
            raise ValueError("candidate must be a configured failover member")
        if len({grant.voter for grant in self.grants}) != 2:
            raise ValueError("promotion proof requires grants from two distinct voters")
        if any(
            grant.epoch != self.epoch
            or grant.term != self.term
            or grant.candidate != self.candidate
            for grant in self.grants
        ):
            raise ValueError("promotion grants must match proof epoch, term, and candidate")
        return self

    def is_current(self, now: datetime | None = None) -> bool:
        return all(grant.is_current(now) for grant in self.grants)

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json"))

    def is_valid_for(
        self,
        membership: FailoverMembershipConfig,
        now: datetime | None = None,
    ) -> bool:
        """Require a current two-member quorum with signatures from declared voters."""
        if membership.epoch != self.epoch or not self.is_current(now):
            return False
        keys = {member.name: member.public_key for member in membership.members}
        return all(
            grant.voter in keys
            and verify_ed25519(grant.canonical_bytes(), grant.signature, keys[grant.voter])
            for grant in self.grants
        )


class ElectionSafetyRecord(FailoverWireModel):
    member: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    epoch: int = Field(ge=1)
    highest_term: int = Field(ge=1)
    outstanding_grants: list[ElectionVoteGrant] = Field(default_factory=list, max_length=2)
    prior_max_lifetime_s: float = Field(ge=1, le=FAILOVER_MAX_AUTHORITY_S)
    restart_quarantine_s: float = Field(default=0, ge=0, le=301)
    snapshot_issued_at: AwareDatetime | None = None
    replay_ids: list[UUID] = Field(default_factory=list, max_length=256)


class FailoverRankIdentity(FailoverWireModel):
    """Opaque process identity: no pid, port, path or weight contents cross this view."""

    rank: int = Field(ge=0, le=1)
    node: Literal["coire-edge-a", "coire-edge-b"]
    generation: str = Field(pattern=r"^[a-f0-9]{64}$")


class FailoverSinglePlacement(FailoverWireModel):
    mode: Literal["single"] = "single"
    instance_id: UUID
    engine_id: UUID
    node: Literal["coire-edge-a", "coire-edge-b"]
    generation: str = Field(pattern=r"^[a-f0-9]{64}$")
    target: InferenceTarget


class FailoverShardedPlacement(FailoverWireModel):
    mode: Literal["tp", "pp"]
    instance_id: UUID
    engine_id: UUID
    group_id: UUID
    generation: str = Field(pattern=r"^[a-f0-9]{64}$")
    target: InferenceTarget
    ranks: list[FailoverRankIdentity] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def complete_rank_set(self) -> Self:
        if {rank.rank for rank in self.ranks} != {0, 1} or {rank.node for rank in self.ranks} != {
            "coire-edge-a",
            "coire-edge-b",
        }:
            raise ValueError("sharded placement requires two distinct ranks on the two Studios")
        if len({rank.generation for rank in self.ranks}) != 2:
            raise ValueError("each rank must have its own process generation")
        if self.target.adapter_id is not None:
            raise ValueError("adapter-backed sharded failover is unsupported")
        return self


FailoverPlacement = Annotated[
    FailoverSinglePlacement | FailoverShardedPlacement, Field(discriminator="mode")
]


class FailoverRankProbe(FailoverWireModel):
    challenge: UUID
    member: Literal["coire-edge-a", "coire-edge-b"]
    epoch: int = Field(ge=1)
    placement: FailoverShardedPlacement
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def bounded_probe(self) -> Self:
        if not 0 < (self.expires_at - self.issued_at).total_seconds() <= 3:
            raise ValueError("rank challenge must expire within three seconds")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class FailoverRankObservation(FailoverWireModel):
    """Challenge-correlated signed live observation, not a durable rank authority."""

    challenge: UUID
    host: Literal["coire-edge-a", "coire-edge-b"]
    epoch: int = Field(ge=1)
    instance_id: UUID
    group_id: UUID
    group_generation: str = Field(pattern=r"^[a-f0-9]{64}$")
    rank: FailoverRankIdentity
    target: InferenceTarget
    healthy: bool
    link_healthy: bool
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def bounded_observation(self) -> Self:
        if (
            self.rank.node != self.host
            or not 0 < (self.expires_at - self.issued_at).total_seconds() <= 3
        ):
            raise ValueError("rank observation must bind its host and expire within three seconds")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class FailoverModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    slug: str
    display_name: str
    entitlement: frozenset[str] = Field(default_factory=frozenset)
    context_window: int = Field(gt=0)
    capabilities: frozenset[Literal["text_chat"]] = Field(default_factory=frozenset)
    placement: FailoverPlacement | None = None
    limits: FailoverLimits = Field(default_factory=FailoverLimits)

    @model_validator(mode="after")
    def placement_matches_model(self) -> Self:
        if self.placement is not None and self.placement.target.model_id != self.id:
            raise ValueError("placement target must match the published model identity")
        if self.capabilities and self.placement is None:
            raise ValueError("eligible capability requires an exact signed placement")
        return self


class FailoverResidentEngine(BaseModel):
    """Safe engine residency view; it deliberately omits pid, port, and local paths."""

    model_config = ConfigDict(extra="forbid")

    engine_id: UUID
    slug: str
    state: Literal["ready"] = "ready"
    placement: FailoverPlacement | None = None


class FailoverRelayIssuance(FailoverWireModel):
    """The local node, not a frontend container, signs request-bound authority."""

    engine_id: UUID
    request: ChatCompletionRequest
    access_assertion: str = Field(min_length=1, max_length=16384)


class FailoverRelayRequest(BaseModel):
    """Inference request relayed through coire-node after snapshot authorization."""

    model_config = ConfigDict(extra="forbid")

    engine_id: UUID
    model_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:@[a-z0-9]+(?:-[a-z0-9]+)*)?$")
    request: ChatCompletionRequest
    authority: FailoverRelayAuthority
    access_assertion: str = Field(min_length=1, max_length=16384, repr=False)


class FailoverAccessVerifier(BaseModel):
    """Public Cloudflare Access verification material safe for a Studio snapshot."""

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    jwks_url: str = Field(min_length=1)


class FailoverEventKind(StrEnum):
    PROMOTED = "promoted"
    DRAINING = "draining"
    FENCED = "fenced"
    HANDBACK = "handback"
    BREAK_GLASS = "break_glass"
    OVERRIDE_INHIBIT = "override_inhibit"
    OVERRIDE_BREAK_GLASS = "override_break_glass"


class FailoverEvent(BaseModel):
    """A bounded, non-authoritative Studio journal entry."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    term: int = Field(ge=1)
    kind: FailoverEventKind
    host: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    occurred_at: datetime
    proof_digest: str = Field(min_length=64, max_length=64)


class FailoverOverrideKind(StrEnum):
    INHIBIT = "inhibit"
    BREAK_GLASS_PROMOTE = "break_glass_promote"


class FailoverOverride(FailoverWireModel):
    model_config = ConfigDict(extra="forbid")

    override_id: UUID = Field(default_factory=uuid4)
    target_host: str = Field(pattern=r"^coire-edge-[ab]$")
    epoch: int = Field(ge=1)
    issued_at: AwareDatetime
    fencing_evidence: str = Field(min_length=1, max_length=1000)
    kind: FailoverOverrideKind
    actor_id: UUID
    reason: str = Field(min_length=1, max_length=1000)
    expires_at: AwareDatetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def bounded_override(self) -> Self:
        lifetime = (self.expires_at - self.issued_at).total_seconds()
        if not self.reason.strip() or not self.fencing_evidence.strip():
            raise ValueError("override reason and fencing evidence cannot be blank")
        if not 0 < lifetime <= FAILOVER_MAX_AUTHORITY_S:
            raise ValueError("override lifetime must be positive and bounded")
        return self

    def is_current(self, now: datetime | None = None) -> bool:
        return self.issued_at <= (now or datetime.now(UTC)) < self.expires_at

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class HandbackNotice(FailoverWireModel):
    """Core asking the elected Studio to drain and fence before the lease returns."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    epoch: int = Field(ge=1)
    term: int = Field(ge=1)
    holder: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    successor: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def bounded_notice(self) -> Self:
        if (
            self.holder == self.successor
            or not 0 < (self.expires_at - self.issued_at).total_seconds() <= 30
        ):
            raise ValueError(
                "hand-back requires distinct participants and a positive lifetime of at most 30 seconds"
            )
        return self

    def is_current(self, now: datetime | None = None) -> bool:
        return self.issued_at <= (now or datetime.now(UTC)) < self.expires_at

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class FailoverEventBatch(BaseModel):
    """Signed journal replay from one Studio. Core is the only writer of audit rows."""

    model_config = ConfigDict(extra="forbid")

    batch_id: UUID = Field(default_factory=uuid4)
    host: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    events: list[FailoverEvent] = Field(min_length=1, max_length=256)
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def events_belong_to_sender(self) -> Self:
        if any(event.host != self.host for event in self.events):
            raise ValueError("all journal events must belong to the signed host")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class FailoverLease(FailoverWireModel):
    """What a Studio may use to prove it is the serving tier. Never a database row."""

    model_config = ConfigDict(extra="forbid")

    holder: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    proof: PromotionProof | None = None
    break_glass: FailoverOverride | None = None
    phase: Literal["serving", "draining"] = "serving"
    drain_deadline: AwareDatetime | None = None

    @model_validator(mode="after")
    def has_one_authority(self) -> Self:
        if (self.proof is None) == (self.break_glass is None):
            raise ValueError("a lease requires exactly one authority: quorum proof or break-glass")
        if (
            self.break_glass is not None
            and self.break_glass.kind is not FailoverOverrideKind.BREAK_GLASS_PROMOTE
        ):
            raise ValueError("a lease override must be a break-glass promotion")
        if self.break_glass is not None and self.break_glass.target_host != self.holder:
            raise ValueError("lease holder must be the override target")
        if (self.phase == "draining") != (self.drain_deadline is not None):
            raise ValueError("draining lease requires exactly one drain deadline")
        if self.proof is not None and self.proof.candidate != self.holder:
            raise ValueError("lease holder must be the proof candidate")
        if self.drain_deadline is not None and self.drain_deadline > self.authority_expires_at:
            raise ValueError("drain deadline cannot extend authority")
        return self

    @property
    def authority_expires_at(self) -> datetime:
        if self.proof is not None:
            return min(grant.expires_at for grant in self.proof.grants)
        if self.break_glass is not None:
            return self.break_glass.expires_at
        return datetime.min.replace(tzinfo=UTC)


class FailoverRelayAuthority(FailoverWireModel):
    request_id: UUID
    holder: str = Field(pattern=r"^coire-edge-[ab]$")
    epoch: int = Field(ge=1)
    lease: FailoverLease
    snapshot_id: UUID
    snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    engine_id: UUID
    model_id: UUID
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    placement: FailoverPlacement | None = None
    identity_subject: str = Field(min_length=1, max_length=256)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_context(self) -> Self:
        if self.lease.holder != self.holder or self.lease.phase != "serving":
            raise ValueError("relay holder must match a serving lease")
        epoch = (
            self.lease.proof.epoch
            if self.lease.proof
            else self.lease.break_glass.epoch
            if self.lease.break_glass
            else None
        )
        if epoch != self.epoch:
            raise ValueError("relay epoch must match authority")
        if self.placement is not None and (
            self.placement.engine_id != self.engine_id
            or self.placement.target.model_id != self.model_id
        ):
            raise ValueError("relay placement must bind the exact engine and model identity")
        lifetime = (self.expires_at - self.issued_at).total_seconds()
        if not 0 < lifetime <= FAILOVER_MAX_AUTHORITY_S:
            raise ValueError("relay lifetime must be positive and bounded")
        if self.expires_at > self.lease.authority_expires_at:
            raise ValueError("relay expiry cannot extend authority")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class FailoverSnapshot(FailoverWireModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    membership: FailoverMembershipConfig
    models: list[FailoverModel] = Field(default_factory=list)
    access_verifier: FailoverAccessVerifier
    subjects: list[FailoverAccessSubject] = Field(default_factory=list, max_length=10000)
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_lifetime_and_models(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("snapshot expiry must be after its issue time")
        if (self.expires_at - self.issued_at).total_seconds() > 120:
            raise ValueError("snapshot authority cannot exceed the 120-second horizon")
        bindings = [(subject.issuer, subject.subject) for subject in self.subjects]
        if len(set(bindings)) != len(bindings):
            raise ValueError("snapshot identity bindings must be unique")
        if len({model.id for model in self.models}) != len(self.models):
            raise ValueError("snapshot model ids must be unique")
        if len({model.slug for model in self.models}) != len(self.models):
            raise ValueError("snapshot model slugs must be unique")
        return self

    def is_current(self, now: datetime | None = None) -> bool:
        return self.expires_at > (now or datetime.now(UTC))

    def is_fresh(self, max_age_s: float, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        age_s = (moment - self.issued_at).total_seconds()
        return 0 <= age_s <= max_age_s and self.expires_at > moment

    def canonical_bytes(self) -> bytes:
        signed = self.model_dump(mode="json", exclude={"signature"})
        return _canonical_json(signed)

    def signature_is_valid(self, trusted_core_public_key: str) -> bool:
        """Check the signature against a separately provisioned core key."""
        if not trusted_core_public_key:
            return False
        core = next(member for member in self.membership.members if member.name == "coire-core")
        return core.public_key == trusted_core_public_key and verify_ed25519(
            self.canonical_bytes(), self.signature, trusted_core_public_key
        )


class FailoverPublicStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: FailoverTier
    elected_host: str | None = None
    reachable_members: frozenset[str] = Field(default_factory=frozenset)
    snapshot_expires_at: datetime | None = None
    admission_available: bool = False
    reason: str = "not_elected"
    authority_expires_at: AwareDatetime | None = None
    unavailable_capabilities: tuple[str, ...] = ()


class FailoverStatus(FailoverPublicStatus):
    """Scoped lifecycle view; browser clients only receive the public base."""

    in_flight: int = Field(default=0, ge=0)


FailoverRelayRequest.model_rebuild()
