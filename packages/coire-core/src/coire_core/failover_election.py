"""Quorum election for the three-member control-plane failover tier.

Core is priority 0, edge-a is priority 1, and edge-b is priority 2. A member campaigns only
when every higher-priority member has been unreachable for the promotion threshold and it can
see a majority, unless an authentic break-glass override is current. An incumbent keeps serving
until a higher-priority member has been healthy for the longer demotion threshold, then drains.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from coire_core.failover_crypto import sign_ed25519, verify_ed25519
from coire_core.failover_telemetry import elections_total, handbacks_total, readiness, tracer
from coire_core.models.failover import (
    FAILOVER_MEMBER_NAMES,
    ElectionVoteGrant,
    ElectionVoteRequest,
    FailoverEvent,
    FailoverEventKind,
    FailoverHeartbeat,
    FailoverLease,
    FailoverMembershipConfig,
    FailoverOverride,
    FailoverOverrideKind,
    HandbackNotice,
    PromotionProof,
)
from coire_core.settings import Settings


class MemberHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"


class ServiceRole(StrEnum):
    STANDBY = "standby"
    CANDIDATE = "candidate"
    ELECTED = "elected"
    DRAINING = "draining"


class MemberObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^coire-(core|edge-[ab])$")
    health: MemberHealth
    since: datetime


class ElectionTimings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion: timedelta
    demotion: timedelta
    lease: timedelta
    drain: timedelta

    def demotion_is_slower(self) -> None:
        if self.demotion <= self.promotion:
            raise ValueError("demotion threshold must exceed the promotion threshold")


class ElectionJournalSink(Protocol):
    def append(self, event: FailoverEvent) -> None: ...


RoleListener = Callable[[ServiceRole, PromotionProof | None], None]


def proof_digest(proof: PromotionProof | None) -> str:
    payload = proof.canonical_bytes() if proof is not None else b"fenced"
    return hashlib.sha256(payload).hexdigest()


def timings_from_settings(settings: Settings) -> ElectionTimings:
    return ElectionTimings(
        promotion=timedelta(seconds=settings.failover_promotion_threshold_s),
        demotion=timedelta(seconds=settings.failover_demotion_threshold_s),
        lease=timedelta(seconds=settings.failover_lease_ttl_s),
        drain=timedelta(seconds=settings.failover_drain_timeout_s),
    )


class ElectionParticipant:
    """One member's view of the election. Callers feed observations; nothing here votes twice."""

    def __init__(
        self,
        *,
        name: str,
        membership: FailoverMembershipConfig,
        private_key_b64: str,
        timings: ElectionTimings,
        journal: ElectionJournalSink | None = None,
        on_change: RoleListener | None = None,
        standing_reservation: bool = False,
    ) -> None:
        if name not in FAILOVER_MEMBER_NAMES:
            raise ValueError("participant is not a configured failover member")
        timings.demotion_is_slower()
        self.name = name
        self.membership = membership
        self.timings = timings
        self.role = ServiceRole.STANDBY
        self.term = 1
        self.proof: PromotionProof | None = None
        self.reservation_held = standing_reservation
        self.in_flight = 0
        self.engines_evicted = 0
        self._private_key = private_key_b64
        self._journal = journal
        self._on_change = on_change
        self._observations: list[MemberObservation] = []
        self._override: FailoverOverride | None = None
        self._received: dict[tuple[int, str], ElectionVoteGrant] = {}
        self._issued: dict[int, ElectionVoteGrant] = {}
        self._closed = False
        self._outage = False
        self._reclaim_since: datetime | None = None
        self._drain_started: datetime | None = None
        self._now = datetime.now(UTC)
        self._published: tuple[ServiceRole, str | None] | None = None

    def observe(
        self,
        observations: list[MemberObservation],
        now: datetime,
        *,
        in_flight: int = 0,
        override: FailoverOverride | None = None,
    ) -> ServiceRole:
        """Advance one poll. Higher-priority degradation never counts as a failure."""
        self._observations = observations
        self._override = override if self._override_is_authentic(override) else None
        self.in_flight = in_flight
        self._now = now
        with tracer.start_as_current_span("coire.failover.election") as span:
            span.set_attribute("coire.failover.host", self.name)
            role = self._advance(now)
            span.set_attribute("coire.failover.role", role.value)
            readiness.set(1 if self.serving(now) else 0, {"host": self.name})
            return role

    def _advance(self, now: datetime) -> ServiceRole:
        if self._inhibit_applies(now):
            self._fence(outage=False)
            self._emit()
            return self.role
        if self.role in (ServiceRole.ELECTED, ServiceRole.DRAINING) and self._higher_recovered(now):
            self._begin_drain(now)
            self._finish_drain_if_idle()
            self._emit()
            return self.role
        if self.role is ServiceRole.DRAINING:
            self._finish_drain_if_idle()
            self._emit()
            return self.role
        if self.role is ServiceRole.ELECTED:
            self._hold_or_fence(now)
            self._emit()
            return self.role
        self._campaign(now)
        self._emit()
        return self.role

    def vote(
        self, epoch: int, term: int, candidate: str, now: datetime
    ) -> ElectionVoteGrant | None:
        """Grant one vote for the current priority leader, replaying the same grant."""
        if epoch != self.membership.epoch or candidate == self.name:
            return None
        if candidate not in FAILOVER_MEMBER_NAMES or self.eligible_leader(now) != candidate:
            return None
        if self.role in (ServiceRole.ELECTED, ServiceRole.DRAINING):
            return None
        if any(
            grant.candidate != candidate and grant.is_current(now)
            for grant in self._issued.values()
        ):
            return None
        if term < self.term:
            return None
        if term > self.term:
            self.term = term
        existing = self._issued.get(self.term)
        if existing is not None and existing.candidate != candidate:
            return None
        if (
            existing is not None
            and existing.is_current(now)
            and existing.expires_at - now > self.timings.lease / 3
        ):
            return existing
        unsigned = ElectionVoteGrant(
            epoch=epoch,
            term=self.term,
            candidate=candidate,
            voter=self.name,
            expires_at=now + self.timings.lease,
            signature="unsigned",
        )
        signed = unsigned.model_copy(
            update={"signature": sign_ed25519(unsigned.canonical_bytes(), self._private_key)}
        )
        self._issued[self.term] = signed
        return signed

    def receive_grant(self, grant: ElectionVoteGrant, now: datetime) -> bool:
        """Accept one current grant for our own term. Older terms cannot displace it."""
        if (
            grant.candidate != self.name
            or grant.epoch != self.membership.epoch
            or grant.term != self.term
            or not grant.is_current(now)
        ):
            return False
        if not verify_ed25519(
            grant.canonical_bytes(), grant.signature, self._public_key(grant.voter)
        ):
            return False
        existing = self._received.get((grant.term, grant.voter))
        if existing is not None and existing.is_current(now):
            if existing.candidate != grant.candidate or grant.expires_at < existing.expires_at:
                return False
            if grant.expires_at == existing.expires_at:
                return existing.signature == grant.signature
        self._received[(grant.term, grant.voter)] = grant
        return True

    def accept_handback(self, notice: HandbackNotice, now: datetime) -> bool:
        """Drain when the priority successor presents a current signed hand-back."""
        if (
            notice.holder != self.name
            or notice.epoch != self.membership.epoch
            or not notice.is_current(now)
            or self._priority(notice.successor) >= self._priority(self.name)
        ):
            return False
        if not verify_ed25519(
            notice.canonical_bytes(), notice.signature, self._public_key(notice.successor)
        ):
            return False
        if self.role is ServiceRole.STANDBY:
            return True
        if self.role not in (ServiceRole.ELECTED, ServiceRole.DRAINING):
            return False
        self._now = now
        self._begin_drain(now)
        self._finish_drain_if_idle()
        self._emit()
        return True

    def serving(self, now: datetime | None = None) -> bool:
        """True only for the current elected lease. Draining and standby both refuse."""
        moment = now or self._now
        if self.role is not ServiceRole.ELECTED:
            return False
        if self._break_glass_ok(moment) and self.proof is None:
            return True
        return self._proof_ok(moment)

    def lease(self) -> FailoverLease | None:
        if self.role is not ServiceRole.ELECTED:
            return None
        if self.proof is not None:
            return FailoverLease(holder=self.name, proof=self.proof)
        if (
            self._override is not None
            and self._override.kind is FailoverOverrideKind.BREAK_GLASS_PROMOTE
        ):
            return FailoverLease(holder=self.name, break_glass=self._override)
        return None

    def eligible_leader(self, now: datetime) -> str | None:
        """The highest-priority member who is not confirmed unreachable past the threshold."""
        for member in sorted(self.membership.members, key=lambda item: item.priority):
            if member.name == self.name:
                return None if self._inhibit_applies(now) else self.name
            status = self._higher_status(member.name, now)
            if status == "down":
                continue
            if status == "wait":
                return None
            return member.name
        return None

    def _campaign(self, now: datetime) -> None:
        leader = self.eligible_leader(now)
        if leader != self.name or not self._may_campaign(now, leader):
            self._reclaim_since = None if leader != self.name else self._reclaim_since
            if self.role is ServiceRole.CANDIDATE:
                self.role = ServiceRole.STANDBY
                self.proof = None
            return
        if self.role is ServiceRole.STANDBY:
            if self._closed:
                self.term += 1
                self._received = {}
                self._issued = {}
            self._closed = False
            self.role = ServiceRole.CANDIDATE
        self._ensure_self_grant(now)
        proof = self._try_proof(now)
        if proof is not None:
            self._promote(proof, break_glass=False)
            return
        if self._break_glass_ok(now):
            self._promote(None, break_glass=True)

    def _may_campaign(self, now: datetime, leader: str | None) -> bool:
        quorum = len(self._visible()) >= 2
        break_glass = self._break_glass_ok(now) and leader == self.name
        if not (quorum or break_glass):
            return False
        if self._outage and self._lower_visible():
            if self._reclaim_since is None:
                self._reclaim_since = now
            if now - self._reclaim_since < self.timings.demotion:
                return False
        else:
            self._reclaim_since = None
        return True

    def _hold_or_fence(self, now: datetime) -> None:
        quorum = len(self._visible()) >= 2
        if not quorum and not self._break_glass_ok(now):
            self._fence(outage=True)
            return
        proof_current = self._proof_ok(now)
        if (
            proof_current
            and self.proof is not None
            and all(grant.expires_at - now > self.timings.lease / 3 for grant in self.proof.grants)
        ):
            return
        if self.proof is None and self._break_glass_ok(now):
            return
        self._ensure_self_grant(now)
        proof = self._try_proof(now)
        if proof is not None:
            self.proof = proof
            return
        if not proof_current:
            self.role = ServiceRole.CANDIDATE
            self.proof = None

    def _promote(self, proof: PromotionProof | None, *, break_glass: bool) -> None:
        first = self.role is not ServiceRole.ELECTED
        self.proof = proof
        self.role = ServiceRole.ELECTED
        self.reservation_held = True
        self._outage = False
        self._reclaim_since = None
        if first:
            kind = FailoverEventKind.BREAK_GLASS if break_glass else FailoverEventKind.PROMOTED
            self._record(kind, proof)
            elections_total.add(1, {"host": self.name, "kind": kind.value})

    def _begin_drain(self, now: datetime) -> None:
        if self.role is ServiceRole.DRAINING:
            return
        previous = self.proof
        self.role = ServiceRole.DRAINING
        self.proof = None
        self._drain_started = now
        self._record(FailoverEventKind.DRAINING, previous)
        handbacks_total.add(1, {"host": self.name})

    def _finish_drain_if_idle(self) -> None:
        if self.role is not ServiceRole.DRAINING:
            return
        if (
            self.in_flight > 0
            and self._drain_started is not None
            and self._now - self._drain_started < self.timings.drain
        ):
            return
        self.reservation_held = False
        self.role = ServiceRole.STANDBY
        self._closed = True
        self._record(FailoverEventKind.HANDBACK, None)

    def _fence(self, *, outage: bool) -> None:
        previous = self.proof
        was_leader = self.role in (ServiceRole.ELECTED, ServiceRole.DRAINING)
        self.role = ServiceRole.STANDBY
        self.proof = None
        self.reservation_held = False
        self._closed = True
        if outage:
            self._outage = True
        if was_leader:
            self._record(FailoverEventKind.FENCED, previous)

    def _ensure_self_grant(self, now: datetime) -> None:
        existing = self._received.get((self.term, self.name))
        if (
            existing is not None
            and existing.is_current(now)
            and existing.candidate == self.name
            and existing.expires_at - now > self.timings.lease / 3
        ):
            return
        unsigned = ElectionVoteGrant(
            epoch=self.membership.epoch,
            term=self.term,
            candidate=self.name,
            voter=self.name,
            expires_at=now + self.timings.lease,
            signature="unsigned",
        )
        self._received[(self.term, self.name)] = unsigned.model_copy(
            update={"signature": sign_ed25519(unsigned.canonical_bytes(), self._private_key)}
        )

    def _try_proof(self, now: datetime) -> PromotionProof | None:
        grants = {
            voter: grant
            for (term, voter), grant in self._received.items()
            if term == self.term
            and grant.candidate == self.name
            and grant.is_current(now)
            and verify_ed25519(grant.canonical_bytes(), grant.signature, self._public_key(voter))
        }
        if self.name not in grants or len(grants) < 2:
            return None
        other = sorted(voter for voter in grants if voter != self.name)[0]
        proof = PromotionProof(
            epoch=self.membership.epoch,
            term=self.term,
            candidate=self.name,
            grants=[grants[self.name], grants[other]],
        )
        return proof if proof.is_valid_for(self.membership, now) else None

    def _proof_ok(self, now: datetime) -> bool:
        return (
            self.proof is not None
            and self.proof.is_current(now)
            and self.proof.is_valid_for(self.membership, now)
        )

    def _visible(self) -> set[str]:
        visible = {self.name}
        for member in self.membership.members:
            if member.name == self.name:
                continue
            observed = self._observed(member.name)
            if observed is not None and observed.health is not MemberHealth.UNREACHABLE:
                visible.add(member.name)
        return visible

    def adopt_term(self, term: int) -> None:
        """Move to a later term after peers have already granted the current one."""
        if term <= self.term or self.role in (ServiceRole.ELECTED, ServiceRole.DRAINING):
            return
        self.term = term
        self._received = {}
        self._issued = {}

    def _higher_status(self, name: str, now: datetime) -> str:
        observed = self._observed(name)
        if observed is None:
            return "wait"
        if observed.health is MemberHealth.UNREACHABLE:
            if now - observed.since >= self.timings.promotion:
                return "down"
            return "wait"
        return "leader"

    def _higher_recovered(self, now: datetime) -> bool:
        mine = self._priority(self.name)
        for member in self.membership.members:
            if member.priority >= mine:
                continue
            observed = self._observed(member.name)
            if (
                observed is not None
                and observed.health is MemberHealth.HEALTHY
                and now - observed.since >= self.timings.demotion
            ):
                return True
        return False

    def _lower_visible(self) -> bool:
        visible = self._visible()
        mine = self._priority(self.name)
        return any(
            member.priority > mine and member.name in visible for member in self.membership.members
        )

    def _inhibit_applies(self, now: datetime) -> bool:
        return (
            self.name != "coire-core"
            and self._override is not None
            and self._override.kind is FailoverOverrideKind.INHIBIT
            and self._override.is_current(now)
        )

    def _break_glass_ok(self, now: datetime) -> bool:
        return (
            self._override is not None
            and self._override.kind is FailoverOverrideKind.BREAK_GLASS_PROMOTE
            and self._override.is_current(now)
        )

    def _override_is_authentic(self, override: FailoverOverride | None) -> bool:
        if override is None:
            return False
        return verify_ed25519(
            override.canonical_bytes(), override.signature, self._public_key("coire-core")
        )

    def _observed(self, name: str) -> MemberObservation | None:
        return next((item for item in self._observations if item.name == name), None)

    def _priority(self, name: str) -> int:
        return next(member.priority for member in self.membership.members if member.name == name)

    def issue_heartbeat(self, now: datetime) -> FailoverHeartbeat:
        """Sign one liveness beat. Peers time the round trip; they do not trust `sent_at` alone."""
        unsigned = FailoverHeartbeat(member=self.name, sent_at=now, signature="unsigned")
        return unsigned.model_copy(
            update={"signature": sign_ed25519(unsigned.canonical_bytes(), self._private_key)}
        )

    def issue_vote_request(self, now: datetime) -> ElectionVoteRequest | None:
        """Ask peers for this term when we are the eligible leader and still need a lease."""
        del now
        if self.role not in (ServiceRole.CANDIDATE, ServiceRole.ELECTED):
            return None
        if self.eligible_leader(self._now) != self.name:
            return None
        unsigned = ElectionVoteRequest(
            epoch=self.membership.epoch,
            term=self.term,
            candidate=self.name,
            signature="unsigned",
        )
        return unsigned.model_copy(
            update={"signature": sign_ed25519(unsigned.canonical_bytes(), self._private_key)}
        )

    def member_public_key(self, name: str) -> str:
        return self._public_key(name)

    def _public_key(self, name: str) -> str:
        return next(member.public_key for member in self.membership.members if member.name == name)

    def _record(self, kind: FailoverEventKind, proof: PromotionProof | None) -> None:
        if self._journal is None:
            return
        self._journal.append(
            FailoverEvent(
                term=self.term,
                kind=kind,
                host=self.name,
                occurred_at=self._now,
                proof_digest=proof_digest(proof),
            )
        )

    def _emit(self) -> None:
        signature = (self.role, None if self.proof is None else self.proof.model_dump_json())
        if signature == self._published:
            return
        self._published = signature
        if self._on_change is not None:
            self._on_change(self.role, self.proof)
