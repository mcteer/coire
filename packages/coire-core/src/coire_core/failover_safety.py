"""Durable local election guards, not replicated application state.

A missing/corrupt record quarantines a voter for the largest supported old lease,
not its possibly reduced current TTL. All guards use monotonic elapsed time; a
wall-clock discontinuity fences the participant for the same conservative bound.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

from coire_core.models.failover import (
    FAILOVER_MAX_AUTHORITY_S,
    ElectionSafetyRecord,
    ElectionVoteGrant,
)


class ElectionSafetyError(ValueError):
    """A required durable safety boundary cannot be established."""


class ElectionSafety:
    def __init__(
        self,
        path: Path,
        *,
        member: str,
        epoch: int,
        lifetime_s: float,
        now: datetime,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self._monotonic = monotonic
        self._wall = now
        self._mono = monotonic()
        self._uncertainty_s = 1.0
        self.failed = False
        self._deadlines: dict[str, float] = {}
        self.record = ElectionSafetyRecord(
            member=member, epoch=epoch, highest_term=1, prior_max_lifetime_s=lifetime_s
        )
        try:
            previous = ElectionSafetyRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if previous.member != member or previous.epoch > epoch:
                raise ElectionSafetyError("trusted membership rollback or identity mismatch")
            self.record = previous.model_copy(
                update={
                    "epoch": epoch,
                    "prior_max_lifetime_s": max(previous.prior_max_lifetime_s, lifetime_s),
                }
            )
            # A recovered grant is conservatively held for its *entire* signed lifetime.
            # Neither a forward clock nor lowered TTL may shorten the guard at restart.
            for grant in previous.outstanding_grants:
                lifetime = (grant.expires_at - grant.issued_at).total_seconds()
                self._deadlines[grant.candidate] = max(
                    self._deadlines.get(grant.candidate, 0),
                    self._mono + lifetime + self._uncertainty_s,
                )
            self._quarantine_until = self._mono + previous.restart_quarantine_s
        except ElectionSafetyError:
            self.failed = True
            self._quarantine_until = float("inf")
        except (OSError, ValueError):
            quarantine = FAILOVER_MAX_AUTHORITY_S + self._uncertainty_s
            self._quarantine_until = self._mono + quarantine
            self.record = self.record.model_copy(update={"restart_quarantine_s": quarantine})
        if not self.failed:
            # Persist a quarantine guard too: repeatedly restarting cannot shorten it.
            self._persist()

    def _persist(self) -> None:
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix=".safety-", delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(self.record.model_dump_json())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            self.failed = True
            raise ElectionSafetyError("election safety persistence unavailable") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def check_clock(self, now: datetime) -> bool:
        moment = self._monotonic()
        elapsed = moment - self._mono
        wall_elapsed = (now - self._wall).total_seconds()
        if elapsed < 0 or abs(wall_elapsed - elapsed) > self._uncertainty_s:
            self._quarantine_until = max(
                self._quarantine_until, moment + FAILOVER_MAX_AUTHORITY_S + self._uncertainty_s
            )
            self.record = self.record.model_copy(update={"restart_quarantine_s": 301.0})
            self._persist()
        elif moment >= self._quarantine_until and self.record.restart_quarantine_s:
            self.record = self.record.model_copy(update={"restart_quarantine_s": 0.0})
            self._persist()
        self._wall = now
        self._mono = moment
        return not self.failed and moment >= self._quarantine_until

    def permits(self, candidate: str, now: datetime) -> bool:
        if not self.check_clock(now):
            return False
        moment = self._monotonic()
        return not any(
            holder != candidate and deadline > moment
            for holder, deadline in self._deadlines.items()
        )

    def grant(self, grant: ElectionVoteGrant, now: datetime) -> None:
        if not self.permits(grant.candidate, now):
            raise ElectionSafetyError("outstanding authority or restart quarantine")
        lifetime = (grant.expires_at - grant.issued_at).total_seconds()
        moment = self._monotonic()
        grants = [
            g
            for g in self.record.outstanding_grants
            if self._deadlines.get(g.candidate, 0) > moment
        ]
        grants = [g for g in grants if g.candidate != grant.candidate]
        self.record = self.record.model_copy(
            update={
                "highest_term": max(self.record.highest_term, grant.term),
                "outstanding_grants": [*grants, grant],
            }
        )
        self._persist()  # Never publish a vote before its safety guard is durable.
        self._deadlines[grant.candidate] = moment + lifetime + self._uncertainty_s

    def remember_override(self, override_id: UUID) -> bool:
        if (
            self.failed
            or override_id in self.record.replay_ids
            or len(self.record.replay_ids) >= 256
        ):
            return False
        self.record = self.record.model_copy(
            update={"replay_ids": [*self.record.replay_ids, override_id]}
        )
        self._persist()
        return True

    def accept_snapshot(self, epoch: int, issued_at: datetime) -> bool:
        previous = self.record.snapshot_issued_at
        if (
            self.failed
            or epoch < self.record.epoch
            or (previous is not None and issued_at < previous)
        ):
            return False
        self.record = self.record.model_copy(
            update={"epoch": epoch, "snapshot_issued_at": issued_at}
        )
        self._persist()
        return True
