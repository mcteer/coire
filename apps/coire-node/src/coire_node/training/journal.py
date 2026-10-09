"""Crash-durable local training intent and aggregate holds (not a wire contract)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgementV2,
    NodeTrainingEvent,
    NodeTrainingEventPage,
    TrainingCommand,
    TrainingPrepareRequest,
)


def command_digest(command: TrainingCommand | CheckpointCommitAcknowledgementV2) -> str:
    """Compare every field, including the advertised digest and execution lease."""
    value = command.model_dump(mode="json")
    # Transport grants are ephemeral credentials, not durable command intent.
    for source in value.get("sources", []):
        source["grant"].pop("secret", None)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class TrainingJournal:
    """One node slot. Unknown and half-spawned attempts retain memory AND disk holds.

    Call prepare under the node's shared admission lock, passing memory available after
    all other counted occupancy and disk available after other subsystem disk holds.
    SQLite FULL transactions persist the hold and intent together before any spawn.
    """

    def __init__(self, root: Path, *, node: str, admission_lock: threading.RLock) -> None:
        if root.is_symlink():
            raise TrainingValidationError("Training journal root is linked")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
            raise TrainingValidationError("Training journal root must be private")
        self.root = root.resolve()
        self.node = node
        self.lock = admission_lock
        path = self.root / "journal.sqlite3"
        if path.is_symlink():
            raise TrainingValidationError("Training journal is linked")
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        path.chmod(0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS attempts ("
            "id TEXT PRIMARY KEY, job TEXT NOT NULL, fence INTEGER NOT NULL, body TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS commands ("
            "id TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT);"
            "CREATE TABLE IF NOT EXISTS events ("
            "attempt TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL,"
            "PRIMARY KEY(attempt,sequence));"
            "CREATE INDEX IF NOT EXISTS attempts_unreleased ON attempts(id) "
            "WHERE json_extract(body, '$.released') IS NOT 1;"
        )

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def records(self) -> list[dict[str, Any]]:
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM attempts")]

    def active_records(self) -> list[dict[str, Any]]:
        """Skip released history without decoding it on every watchdog tick.

        The partial index follows transactional journal updates. Missing release proof
        remains included; neither unknown owners nor retained disk holds disappear.
        """
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM attempts WHERE json_extract(body, '$.released') IS NOT 1"
                )
            ]

    def get(self, attempt_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT body FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise TrainingConflict("Training attempt is not prepared")
            return dict(json.loads(row[0]))

    def save(self, value: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE attempts SET body=? WHERE id=?",
            (json.dumps(value, sort_keys=True, allow_nan=False), value["attempt_id"]),
        )

    def accept(
        self,
        command: TrainingCommand | CheckpointCommitAcknowledgementV2,
        *,
        require_lease: bool = True,
    ) -> str | None:
        """Within a transaction: fence before replay, never let replay widen authority."""
        command = type(command).model_validate(command.model_dump(mode="json"))
        if command.node != self.node:
            raise TrainingConflict("Command targets another Studio")
        latest = self.db.execute(
            "SELECT MAX(fence) FROM attempts WHERE job=?", (command.job_id,)
        ).fetchone()[0]
        if latest is not None and command.fence < latest:
            raise TrainingConflict("Training command is fenced")
        if require_lease:
            seconds = (command.lease_expires_at - datetime.now(UTC)).total_seconds()
            if not 0 < seconds <= 30:
                raise TrainingConflict("Execution lease must expire within thirty seconds")
        existing = self.db.execute(
            "SELECT digest, receipt FROM commands WHERE id=?", (str(command.command_id),)
        ).fetchone()
        digest = command_digest(command)
        if existing is not None:
            if existing[0] != digest:
                raise TrainingConflict("Immutable training command changed")
            return existing[1] if existing[1] is not None else ""
        self.db.execute(
            "INSERT INTO commands(id,digest) VALUES (?,?)", (str(command.command_id), digest)
        )
        return None

    def receipt(
        self, command: TrainingCommand | CheckpointCommitAcknowledgementV2, encoded: str
    ) -> None:
        self.db.execute(
            "UPDATE commands SET receipt=? WHERE id=?", (encoded, str(command.command_id))
        )

    def held_bytes(self) -> tuple[int, int]:
        items = self.records()
        return (
            sum(item["memory_bytes"] for item in items if not item["released"]),
            sum(item["disk_bytes"] for item in items),
        )

    def hold_input_bytes(
        self, attempt_id: str, size: int, *, disk_available: int, source_id: str | None = None
    ) -> None:
        """Reserve private source/index bytes before staging, under shared admission lock."""
        if not 0 < size <= 640 * 1024**2:
            raise TrainingValidationError(
                "Training input bytes exceed the bounded source/index size"
            )
        with self.transaction():
            value = self.get(attempt_id)
            if (
                value["released"]
                or value["spawn_nonce"] is not None
                or value["liveness"] != "prepared"
            ):
                raise TrainingConflict("Training inputs cannot change after spawning or releasing")
            holds = value.setdefault("input_holds", {})
            prior = (
                holds.get(source_id, 0) if source_id is not None else value.get("input_bytes", 0)
            )
            if prior and size > prior:
                raise TrainingConflict("Input disk hold is immutable")
            if not prior:
                if self.held_bytes()[1] + size > disk_available:
                    raise TrainingValidationError("Aggregate training input disk hold cannot fit")
                if source_id is not None:
                    holds[source_id] = size
                else:
                    value["input_bytes"] = size
                value["disk_bytes"] += size
                self.save(value)

    def append_event(self, event: NodeTrainingEvent) -> None:
        event = NodeTrainingEvent.model_validate(event.model_dump(mode="json"))
        encoded = event.model_dump_json()
        if len(encoded.encode()) > 64 * 1024**2:
            raise TrainingValidationError("Training event exceeds its byte bound")
        with self.transaction():
            value = self.get(event.attempt_id)
            prepared = TrainingPrepareRequest.model_validate(value["prepare"])
            latest = self.db.execute(
                "SELECT MAX(fence) FROM attempts WHERE job=?", (event.job_id,)
            ).fetchone()[0]
            if (
                event.job_id != prepared.job_id
                or event.fence != prepared.fence
                or event.fence != latest
                or value["released"]
                or value["spawn_nonce"] is None
                or value["liveness"] in {"prepared", "stopped"}
                or datetime.fromisoformat(value["lease_expires_at"]) <= datetime.now(UTC)
            ):
                raise TrainingConflict("Worker event has no current fenced execution authority")
            if event.payload.kind == "progress" and (
                event.payload.metric.job_id != event.job_id
                or event.payload.metric.attempt_id != event.attempt_id
                or event.payload.metric.update != event.update
            ):
                raise TrainingConflict("Worker metric differs from event identity")
            if event.payload.kind == "checkpoint_staged" and (
                event.payload.manifest.job_id != event.job_id
                or event.payload.manifest.attempt_id != event.attempt_id
                or event.payload.manifest.fence != event.fence
                or event.payload.manifest.update != event.update
            ):
                raise TrainingConflict("Worker checkpoint differs from event identity")
            if event.payload.kind == "checkpoint_rank_staged" and (
                event.payload.component.job_id != event.job_id
                or event.payload.component.attempt_id != event.attempt_id
                or event.payload.component.fence != event.fence
                or event.payload.component.update != event.update
                or event.payload.component.rank != prepared.rank
                or event.payload.component.world_size != prepared.world_size
                or event.payload.component.runtime_sha256 != prepared.resolved.runtime_sha256
                or event.payload.component.resolved_spec_sha256
                != hashlib.sha256(
                    json.dumps(
                        prepared.resolved.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            ):
                raise TrainingConflict("Worker rank component differs from owned event scope")
            prior = self.db.execute(
                "SELECT body FROM events WHERE attempt=? AND sequence=?",
                (event.attempt_id, event.sequence),
            ).fetchone()
            if prior:
                if prior[0] != encoded:
                    raise TrainingConflict("Worker event sequence is immutable")
                return
            if event.update < value.get("update", 0):
                raise TrainingConflict("Worker event cannot rewind an owned execution update")
            count, used, sequence = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(body)),0), COALESCE(MAX(sequence),0) FROM events WHERE attempt=?",
                (event.attempt_id,),
            ).fetchone()
            if event.sequence != sequence + 1:
                raise TrainingConflict("Worker events must be contiguous")
            if count >= 300_005 or used + len(encoded.encode()) > 512 * 1024**2:
                raise TrainingValidationError("Private training event spool is full")
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?)", (event.attempt_id, event.sequence, encoded)
            )
            value["update"] = event.update
            if event.payload.kind == "checkpoint_staged":
                value["latest_staged_artifact_id"] = str(event.payload.manifest.artifact_id)
            elif event.payload.kind == "checkpoint_rank_staged":
                value["latest_staged_artifact_id"] = str(event.payload.component.artifact_id)
            if event.payload.kind == "failure":
                value["reason"] = event.payload.reason
            self.save(value)

    def events(self, attempt_id: str, after: int = 0) -> NodeTrainingEventPage:
        if after < 0:
            raise TrainingValidationError("Event cursor must be nonnegative")
        with self.lock:
            self.get(attempt_id)
            values = self.db.execute(
                "SELECT body FROM events WHERE attempt=? AND sequence>? ORDER BY sequence LIMIT 100",
                (attempt_id, after),
            ).fetchall()
            items = [NodeTrainingEvent.model_validate_json(row[0]) for row in values]
            return NodeTrainingEventPage(
                items=items, next_sequence=items[-1].sequence if items else after
            )

    def next_sequence(self, attempt_id: str) -> int:
        with self.lock:
            self.get(attempt_id)
            row = self.db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM events WHERE attempt=?", (attempt_id,)
            ).fetchone()
            return int(row[0]) + 1

    def prepare(
        self,
        command: TrainingPrepareRequest,
        *,
        memory_available: int,
        disk_available: int,
        disk_floor: int = 20 * 1024**3,
    ) -> dict[str, Any]:
        with self.transaction():
            replay = self.accept(command)
            if replay is not None:
                return self.get(command.attempt_id)
            if any(not item["released"] for item in self.records()):
                raise TrainingConflict("Studio training slot is held")
            if any(item["attempt_id"] == command.attempt_id for item in self.records()):
                raise TrainingConflict("Attempt preparation is immutable")
            envelope = command.resolved.resource_envelope
            memory = envelope.memory_bytes
            # Retained full states plus staging and mirror headroom, never just weights.
            disk = envelope.checkpoint_bytes * (
                command.resolved.spec.output.keep_last_checkpoints + 2
            )
            _, retained_disk = self.held_bytes()
            if (
                disk > 20 * 1024**3
                or memory > memory_available
                or disk + retained_disk + disk_floor > disk_available
            ):
                raise TrainingValidationError(
                    "Training memory or aggregate disk headroom unavailable"
                )
            value = {
                "attempt_id": command.attempt_id,
                "prepare": command.model_dump(mode="json"),
                "memory_bytes": memory,
                "disk_bytes": disk,
                "released": False,
                "liveness": "prepared",
                "spawn_nonce": None,
                "pid": None,
                "process_create_time": None,
                "lease_expires_at": command.lease_expires_at.isoformat(),
                "pause_deadline": None,
                "reason": None,
                "artifact_tracking_version": 1,
                "latest_staged_artifact_id": None,
                "latest_checkpoint_id": None,
            }
            self.db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?)",
                (command.attempt_id, command.job_id, command.fence, json.dumps(value)),
            )
            self.receipt(command, "prepared")
            return value

    def scoped(
        self, command: TrainingCommand | CheckpointCommitAcknowledgementV2
    ) -> dict[str, Any]:
        value = self.get(command.attempt_id)
        prepared = TrainingPrepareRequest.model_validate(value["prepare"])
        if isinstance(command, TrainingPrepareRequest) and command_digest(
            command
        ) != command_digest(prepared):
            raise TrainingConflict("Training preparation changed after reservation")
        if any(
            getattr(command, field) != getattr(prepared, field)
            for field in (
                "job_id",
                "attempt_id",
                "fence",
                "node",
                "rank",
                "world_size",
                "request_sha256",
            )
        ):
            raise TrainingConflict("Command differs from prepared execution scope")
        return value

    def release_after_death(self, attempt_id: str) -> bool:
        with self.transaction():
            value = self.get(attempt_id)
            if value["liveness"] != "stopped":
                raise TrainingConflict("Unknown or live process cannot release a training hold")
            changed = not value["released"]
            value["released"] = True
            self.save(value)
            return changed
