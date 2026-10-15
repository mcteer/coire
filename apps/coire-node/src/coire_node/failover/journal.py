"""Bounded, non-lossy security evidence with explicit acknowledged compaction."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from coire_core.failover_telemetry import audit_available, audit_utilization
from coire_core.models.failover import FailoverEvent, FailoverEventKind


class ElectionJournalError(ValueError):
    """Required audit evidence cannot be retained; admission must stop."""


class ElectionJournal:
    """Unacknowledged entries are never evicted. Terminal fencing has reserved capacity."""

    _locks: ClassVar[dict[Path, threading.RLock]] = {}
    _locks_guard = threading.Lock()
    _terminal = frozenset({FailoverEventKind.FENCED, FailoverEventKind.HANDBACK})

    def __init__(self, path: Path, *, capacity: int = 256) -> None:
        if capacity < 1 or capacity > 256:
            raise ValueError("journal capacity must be between one and 256")
        self._path = path
        self._capacity = capacity
        self._terminal_path = path.with_name(path.name + ".terminal")
        with self._locks_guard:
            self._lock = self._locks.setdefault(path.resolve(), threading.RLock())

    def entries(self) -> list[FailoverEvent]:
        with self._lock:
            return self._read(self._path, self._capacity) + self._read(self._terminal_path, 2)

    def _read(self, path: Path, capacity: int) -> list[FailoverEvent]:
        with self._lock:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, list) or len(payload) > capacity:
                    raise ElectionJournalError("election journal exceeds its bounded capacity")
                entries = [FailoverEvent.model_validate(item) for item in payload]
                if len({event.event_id for event in entries}) != len(entries):
                    raise ElectionJournalError("election journal contains duplicate IDs")
                return entries
            except FileNotFoundError:
                return []
            except (OSError, ValueError) as exc:
                raise ElectionJournalError("election journal is malformed or unavailable") from exc

    def admission_available(self) -> bool:
        labels = {"host": self._path.name.split(".")[0]}
        try:
            entries = self.entries()
            ordinary = sum(event.kind not in self._terminal for event in entries)
            available = (
                sum(event.kind not in self._terminal for event in entries) < self._capacity
                and sum(event.kind in self._terminal for event in entries) < 2
            )
            audit_utilization.set(ordinary / self._capacity, labels)
            audit_available.set(int(available), labels)
            return available
        except ElectionJournalError:
            audit_available.set(0, labels)
            return False

    def append(self, event: FailoverEvent) -> None:
        with self._lock:
            terminal = event.kind in self._terminal
            path = self._terminal_path if terminal else self._path
            entries = self._read(path, 2 if terminal else self._capacity)
            if any(entry.event_id == event.event_id for entry in entries):
                return
            if len(entries) >= (2 if terminal else self._capacity):
                raise ElectionJournalError("unacknowledged election journal is full")
            self._write(path, [*entries, event])

    def acknowledge(self, committed_ids: list[UUID]) -> None:
        with self._lock:
            committed = set(committed_ids)
            entries = self.entries()
            if not committed <= {entry.event_id for entry in entries}:
                raise ElectionJournalError("acknowledgement contains unknown event IDs")
            retained = [entry for entry in entries if entry.event_id not in committed]
            self._write(
                self._path, [entry for entry in retained if entry.kind not in self._terminal]
            )
            self._write(
                self._terminal_path, [entry for entry in retained if entry.kind in self._terminal]
            )

    def _write(self, path: Path, entries: list[FailoverEvent]) -> None:
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._path.parent, prefix=".journal-", delete=False
            ) as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(
                    [entry.model_dump(mode="json") for entry in entries],
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise ElectionJournalError("election journal persistence unavailable") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
