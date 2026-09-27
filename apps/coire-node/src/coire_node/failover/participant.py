"""Studio election member. The lease file is the only signal the failover frontend trusts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from coire_core.failover_crypto import verify_ed25519
from coire_core.failover_election import ElectionParticipant, ElectionTimings, ServiceRole
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
    ) -> None:
        self.proof_path = proof_path
        self._delivered_override: FailoverOverride | None = None
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
        )

    def vote(
        self, epoch: int, term: int, candidate: str, now: datetime
    ) -> ElectionVoteGrant | None:
        if now < self._restart_fence_until:
            return None
        return super().vote(epoch, term, candidate, now)

    def _may_campaign(self, now: datetime, leader: str | None) -> bool:
        return now >= self._restart_fence_until and super()._may_campaign(now, leader)

    def deliver_override(self, override: FailoverOverride) -> bool:
        core_key = self.member_public_key("coire-core")
        if not override.is_current() or not verify_ed25519(
            override.canonical_bytes(), override.signature, core_key
        ):
            return False
        if (
            self._delivered_override is not None
            and self._delivered_override.signature == override.signature
        ):
            return True
        self._delivered_override = override
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
                )
            )
        return True

    def active_override(self) -> FailoverOverride | None:
        override = self._delivered_override
        if override is None or not override.is_current(datetime.now(UTC)):
            self._delivered_override = None
            return None
        return override

    def publish_lease(self, role: ServiceRole, proof: PromotionProof | None) -> None:
        """Replace the lease file only while elected. Every other role deletes it."""
        del role, proof
        lease = self.lease()
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
