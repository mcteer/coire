"""Atomic, signed publication of the Studio's read-only failover snapshot."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from coire_core.failover_crypto import sign_ed25519
from coire_core.failover_snapshot import SnapshotError, load_verified_snapshot
from coire_core.models.failover import FailoverSnapshot

__all__ = ["SnapshotError", "SnapshotPublisher", "load_verified_snapshot"]


class SnapshotPublisher:
    """Publishes a complete replacement snapshot, never an in-place update."""

    def __init__(self, path: Path, private_key_b64: str) -> None:
        self._path = path
        self._private_key_b64 = private_key_b64

    def publish(self, snapshot: FailoverSnapshot) -> FailoverSnapshot:
        """Sign and atomically replace the snapshot file with restrictive permissions."""
        unsigned = snapshot.model_copy(update={"signature": "unsigned"})
        signed = unsigned.model_copy(
            update={"signature": sign_ed25519(unsigned.canonical_bytes(), self._private_key_b64)}
        )
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self._path.parent, prefix=".snapshot-", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(signed.model_dump(mode="json"), handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._path)
        return signed


# Re-exported for existing callers. The check itself lives in coire-core so Studios can load
# a snapshot without importing the control plane.
