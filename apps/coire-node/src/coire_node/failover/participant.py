"""Studio election member. The lease file is the only signal the failover frontend trusts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from coire_core.failover_crypto import verify_ed25519
from coire_core.failover_election import ElectionParticipant, ElectionTimings, ServiceRole
from coire_core.failover_telemetry import drain_remaining
from coire_core.models.failover import (
    ElectionVoteGrant,
    FailoverEvent,
    FailoverEventKind,
    FailoverMembershipConfig,
    FailoverOverride,
    FailoverOverrideKind,
    PromotionProof,
)
from coire_node.failover.journal import ElectionJournal


class StudioParticipant(ElectionParticipant):
    """A Studio member with a standing reservation and a fail-closed lease file."""

    def __init__(
        self,
        *,
        name: str,
        membership: FailoverMembershipConfig,
        private_key_b64: str,
        timings: ElectionTimings,
        proof_path: Path,
        journal: ElectionJournal | None = None,
        restart_hold: bool = False,
        safety_path: Path | None = None,
    ) -> None:
        self.proof_path = proof_path
        self._delivered_override: FailoverOverride | None = None
        self._published_role = ServiceRole.STANDBY
        self._restart_fence_until = (
            datetime.now(UTC) + timings.lease if restart_hold else datetime.min.replace(tzinfo=UTC)
        )
        if restart_hold:
            proof_path.unlink(missing_ok=True)
        super().__init__(
            name=name,
            membership=membership,
            private_key_b64=private_key_b64,
            timings=timings,
            journal=journal,
            on_change=self.publish_lease,
            standing_reservation=True,
            safety_path=safety_path,
        )

    def vote(
        self,
        epoch: int,
        term: int,
        candidate: str,
        now: datetime,
        request_id: UUID | None = None,
    ) -> ElectionVoteGrant | None:
        if now < self._restart_fence_until:
            return None
        return super().vote(epoch, term, candidate, now, request_id=request_id)

    def _may_campaign(self, now: datetime, leader: str | None) -> bool:
        return now >= self._restart_fence_until and super()._may_campaign(now, leader)

    def deliver_override(self, override: FailoverOverride) -> bool:
        core_key = self.member_public_key("coire-core")
        if (
            not override.is_current()
            or override.target_host != self.name
            or override.epoch != self.membership.epoch
        ) or not verify_ed25519(override.canonical_bytes(), override.signature, core_key):
            return False
        if (
            self._delivered_override is not None
            and self._delivered_override.signature == override.signature
        ):
            return False
        if self.safety is not None and not self.safety.remember_override(override.override_id):
            return False
        if self._journal is not None:
            kind = (
                FailoverEventKind.OVERRIDE_INHIBIT
                if override.kind is FailoverOverrideKind.INHIBIT
                else FailoverEventKind.OVERRIDE_BREAK_GLASS
            )
            self._journal.append(
                FailoverEvent(
                    term=self.term,
                    kind=kind,
                    host=self.name,
                    occurred_at=datetime.now(UTC),
                    proof_digest=hashlib.sha256(override.canonical_bytes()).hexdigest(),
                    override=override,
                )
            )
        self._delivered_override = override
        return True

    def active_override(self) -> FailoverOverride | None:
        override = self._delivered_override
        if override is None or not override.is_current(datetime.now(UTC)):
            self._delivered_override = None
            return None
        return override

    def publish_lease(self, role: ServiceRole, proof: PromotionProof | None) -> None:
        """Publish serving or bounded drain authority; standby removes the file."""
        del proof
        if (
            role is ServiceRole.STANDBY
            and self._published_role in (ServiceRole.ELECTED, ServiceRole.DRAINING)
            and self._delivered_override is not None
            and self._delivered_override.kind is FailoverOverrideKind.BREAK_GLASS_PROMOTE
        ):
            self._delivered_override = None
        self._published_role = role
        lease = self.lease()
        drain_remaining.set(
            max(0, (lease.drain_deadline - datetime.now(UTC)).total_seconds())
            if lease is not None and lease.drain_deadline is not None
            else 0,
            {"host": self.name},
        )
        if lease is None:
            self.proof_path.unlink(missing_ok=True)
            return
        self.proof_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.proof_path.parent.chmod(0o755)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.proof_path.parent, prefix=".lease-", delete=False
        ) as handle:
            # A separate non-root frontend container reads this public, signed proof.
            os.fchmod(handle.fileno(), 0o644)
            handle.write(lease.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, self.proof_path)
