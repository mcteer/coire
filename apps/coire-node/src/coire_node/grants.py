"""Transfer grants for peer replication.

A replica needs to read one model's files from the origin. It cannot present the origin's
bearer token — each node holds only its own, and copying node A's token to node B would widen
the static-token exposure ADR-0001 already apologises for.

So the control plane mints a grant: 32 random bytes, scoped to **one model on one node**,
expiring, revoked when the job ends. It is the smallest credential that authorises exactly the
transfer that has to happen, and it authorises nothing else — not another model, not a write,
not the health endpoint.
"""

from __future__ import annotations

import hmac
import logging
import threading
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

MIN_GRANT_LENGTH = 32


class Grants:
    """Live transfer grants. Thread-safe: the export routes and the sweep both touch it."""

    def __init__(self) -> None:
        self._grants: dict[str, tuple[str, datetime]] = {}
        self._lock = threading.Lock()

    def register(self, grant: str, slug: str, expires_at: datetime) -> None:
        if len(grant) < MIN_GRANT_LENGTH:
            raise ValueError("a transfer grant must be at least 32 characters")
        with self._lock:
            self._grants[grant] = (slug, expires_at.astimezone(UTC))
        logger.info("registered a transfer grant for %s until %s", slug, expires_at)

    def resolve(self, grant: str) -> str | None:
        """The slug a grant authorises, or None if it is unknown, expired, or revoked.

        Compared in constant time against every live grant. The set is tiny — one per
        in-flight replication — so the cost is nil and a timing oracle on a credential is not
        worth leaving open.
        """
        now = datetime.now(UTC)
        with self._lock:
            for candidate, (slug, expires) in list(self._grants.items()):
                if hmac.compare_digest(candidate, grant):
                    if expires <= now:
                        del self._grants[candidate]
                        logger.info("grant for %s has expired", slug)
                        return None
                    return slug
        return None

    def revoke_for(self, slug: str) -> int:
        with self._lock:
            stale = [g for g, (s, _) in self._grants.items() if s == slug]
            for grant in stale:
                del self._grants[grant]
        if stale:
            logger.info("revoked %d grant(s) for %s", len(stale), slug)
        return len(stale)

    def sweep(self) -> int:
        """Drop expired grants. Called periodically so a long-lived agent does not accumulate
        them; `resolve` would refuse them anyway."""
        now = datetime.now(UTC)
        with self._lock:
            expired = [g for g, (_, e) in self._grants.items() if e <= now]
            for grant in expired:
                del self._grants[grant]
        return len(expired)

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)
