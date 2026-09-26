"""Strict contracts for the stateless control-plane failover tier."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coire_core.failover_crypto import verify_ed25519
from coire_core.models.gateway import ChatCompletionRequest

FAILOVER_MEMBER_NAMES = frozenset({"coire-core", "coire-edge-a", "coire-edge-b"})


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


class FailoverMembershipConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    members: list[FailoverMember] = Field(min_length=3, max_length=3)
    signing_key_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def fixed_three_member_cluster(self) -> Self:
        if {member.name for member in self.members} != FAILOVER_MEMBER_NAMES:
            raise ValueError("membership must contain core, edge-a, and edge-b exactly once")
        if len({member.priority for member in self.members}) != len(self.members):
            raise ValueError("member priorities must be unique")
        return self


class FailoverHeartbeat(BaseModel):
    """A signed liveness beat. Latency of the exchange is measured by the caller."""

    model_config = ConfigDict(extra="forbid")

    member: str
    sent_at: datetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def member_is_known(self) -> Self:
        if self.member not in FAILOVER_MEMBER_NAMES:
            raise ValueError("heartbeat member must be a configured failover member")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class ElectionVoteRequest(BaseModel):
    """A candidate asking one peer for its single grant in a term."""

    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    term: int = Field(ge=1)
    candidate: str
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def candidate_is_known(self) -> Self:
        if self.candidate not in FAILOVER_MEMBER_NAMES:
            raise ValueError("candidate must be a configured failover member")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class ElectionVoteGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    term: int = Field(ge=1)
    candidate: str
    voter: str
    expires_at: datetime
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def member_names_are_known(self) -> Self:
        if self.candidate not in FAILOVER_MEMBER_NAMES or self.voter not in FAILOVER_MEMBER_NAMES:
            raise ValueError("candidate and voter must be configured failover members")
        return self

    def is_current(self, now: datetime | None = None) -> bool:
        """Whether the grant can contribute to a lease at ``now``."""
        now = now or datetime.now(UTC)
        return self.expires_at > now

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class PromotionProof(BaseModel):
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


class FailoverModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    slug: str
    display_name: str
    entitlement: frozenset[str] = Field(default_factory=frozenset)
    context_window: int = Field(gt=0)


class FailoverResidentEngine(BaseModel):
    """Safe engine residency view; it deliberately omits pid, port, and local paths."""

    model_config = ConfigDict(extra="forbid")

    engine_id: UUID
    slug: str
    state: Literal["ready"] = "ready"


class FailoverRelayRequest(BaseModel):
    """Inference request relayed through coire-node after snapshot authorization."""

    model_config = ConfigDict(extra="forbid")

    engine_id: UUID
    model_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:@[a-z0-9]+(?:-[a-z0-9]+)*)?$")
    request: ChatCompletionRequest


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


class FailoverOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FailoverOverrideKind
    actor_id: UUID
    reason: str = Field(min_length=1, max_length=1000)
    expires_at: datetime
    signature: str = Field(min_length=1)

    def is_current(self, now: datetime | None = None) -> bool:
        return self.expires_at > (now or datetime.now(UTC))

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class HandbackNotice(BaseModel):
    """Core asking the elected Studio to drain and fence before the lease returns."""

    model_config = ConfigDict(extra="forbid")

    epoch: int = Field(ge=1)
    term: int = Field(ge=1)
    holder: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    successor: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    expires_at: datetime
    signature: str = Field(min_length=1)

    def is_current(self, now: datetime | None = None) -> bool:
        return self.expires_at > (now or datetime.now(UTC))

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class FailoverEventBatch(BaseModel):
    """Signed journal replay from one Studio. Core is the only writer of audit rows."""

    model_config = ConfigDict(extra="forbid")

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


class FailoverLease(BaseModel):
    """What a Studio may use to prove it is the serving tier. Never a database row."""

    model_config = ConfigDict(extra="forbid")

    holder: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    proof: PromotionProof | None = None
    break_glass: FailoverOverride | None = None

    @model_validator(mode="after")
    def has_one_authority(self) -> Self:
        if self.proof is None and self.break_glass is None:
            raise ValueError("a lease needs a quorum proof or a break-glass override")
        if self.proof is not None and self.proof.candidate != self.holder:
            raise ValueError("lease holder must be the proof candidate")
        return self


class FailoverSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    issued_at: datetime
    expires_at: datetime
    membership: FailoverMembershipConfig
    models: list[FailoverModel] = Field(default_factory=list)
    access_verifier: FailoverAccessVerifier
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_lifetime_and_models(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("snapshot expiry must be after its issue time")
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


class FailoverStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: FailoverTier
    elected_host: str | None = None
    reachable_members: frozenset[str] = Field(default_factory=frozenset)
    snapshot_expires_at: datetime | None = None
    in_flight: int = Field(default=0, ge=0)
    unavailable_capabilities: tuple[str, ...] = ()
