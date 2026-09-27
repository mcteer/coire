"""Bounded append-only election journal retained only for later audit reconciliation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from coire_core.models.failover import FailoverEvent


class ElectionJournal:
    """A local, bounded journal; callers must never treat it as authority."""

    def __init__(self, path: Path, *, capacity: int = 256) -> None:
        if capacity < 1:
            raise ValueError("journal capacity must be positive")
        self._path = path
        self._capacity = capacity

    def entries(self) -> list[FailoverEvent]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return [FailoverEvent.model_validate(item) for item in payload]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("election journal is malformed") from exc

    def append(self, event: FailoverEvent) -> None:
        entries = self.entries()
        if any(entry.event_id == event.event_id for entry in entries):
            return
        entries.append(event)
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self._path.parent, prefix=".journal-", delete=False
        ) as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(
                [entry.model_dump(mode="json") for entry in entries[-self._capacity :]],
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(temporary_path, self._path)
