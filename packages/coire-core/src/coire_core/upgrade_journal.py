"""Private, bounded, fsynced upgrade data shared by independent recovery owners."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import UpgradeWire

if TYPE_CHECKING:
    from coire_core.models.upgrades import (
        UpgradeExecutionPlan,
        UpgradeJournalEntry,
        UpgradeReceipt,
        UpgradeReceiptAck,
    )

MAX_DOCUMENT_BYTES = 512 * 1024


class PrivateUpgradeStore:
    def __init__(self, root: Path, *, create: bool = True) -> None:
        if root.is_symlink():
            raise UpgradeUnavailable("Upgrade journal root is linked")
        if create:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            details = root.stat()
        except FileNotFoundError:
            raise UpgradeUnavailable("Retained upgrade journal root is absent") from None
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise UpgradeUnavailable("Upgrade journal root is not private")
        self.root = root.resolve()

    def path(self, key: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key) is None:
            raise UpgradeConflict("Upgrade data key is invalid")
        return self.root / key

    def read[T: UpgradeWire](self, key: str, model: type[T]) -> T | None:
        path = self.path(key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise UpgradeUnavailable("Upgrade data cannot be read safely") from None
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
                or details.st_nlink != 1
                or details.st_size > MAX_DOCUMENT_BYTES
            ):
                raise UpgradeUnavailable("Upgrade data ownership or size differs")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(MAX_DOCUMENT_BYTES + 1)
            if len(data) > MAX_DOCUMENT_BYTES:
                raise UpgradeUnavailable("Upgrade data exceeds its bound")
            after = os.fstat(fd)
            observed = path.lstat()
            if (
                details.st_dev,
                details.st_ino,
                details.st_size,
                details.st_mtime_ns,
                details.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or (observed.st_dev, observed.st_ino) != (details.st_dev, details.st_ino):
                raise UpgradeUnavailable("Upgrade data changed during verification")

            def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = item
                return result

            try:
                # Retain JSON-mode validation (timestamps are encoded strings),
                # but reject duplicate authority fields before Pydantic parsing.
                json.loads(data, object_pairs_hook=unique_fields)
                return model.model_validate_json(data)
            except ValueError:
                raise UpgradeUnavailable("Upgrade data is corrupt") from None
        finally:
            os.close(fd)

    def write(self, key: str, value: UpgradeWire, *, replace: bool = True) -> None:
        path = self.path(key)
        data = value.model_dump_json().encode()
        if len(data) > MAX_DOCUMENT_BYTES:
            raise UpgradeUnavailable("Upgrade data exceeds its bound")
        pending = self.root / f".{key}.{uuid4().hex}.pending"
        try:
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                if path.is_symlink():
                    raise UpgradeUnavailable("Upgrade data target is linked")
                os.replace(pending, path)
            else:
                os.link(pending, path, follow_symlinks=False)
                pending.unlink()
            self.sync()
        except FileExistsError:
            raise UpgradeConflict("Upgrade data already exists") from None
        except OSError:
            raise UpgradeUnavailable("Upgrade data could not be persisted") from None
        finally:
            pending.unlink(missing_ok=True)

    def sync(self) -> None:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def ownership(self, key: str = "owner.lock", *, wait_s: float = 0) -> Iterator[int]:
        if not 0 <= wait_s <= 1:
            raise ValueError("Private journal lock wait must be bounded to one second")
        path = self.path(key)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise UpgradeUnavailable("Upgrade ownership file is unavailable") from None
        try:
            details = os.fstat(fd)
            if (
                details.st_uid != os.getuid()
                or details.st_mode & 0o077
                or not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
            ):
                raise UpgradeUnavailable("Upgrade ownership file is unsafe")
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise UpgradeConflict("Another recovery owner is active") from None
                    time.sleep(min(0.01, remaining))
            yield fd
        finally:
            os.close(fd)


class UpgradeReceiptJournal:
    """Hash-linked, append-only local receipts stay retained across upload loss."""

    def __init__(self, root: Path, plan: UpgradeExecutionPlan) -> None:
        from coire_core.models.upgrades import UpgradeExecutionPlan

        normalized = UpgradeExecutionPlan.model_validate(
            plan.model_dump(
                mode="json", exclude={"activation_grant", "activation_receipt", "peer_inventory"}
            )
        )
        self.store = PrivateUpgradeStore(root)
        with self.store.ownership("journal.lock", wait_s=1):
            existing = self.store.read("plan.json", UpgradeExecutionPlan)
            if existing is None:
                self.store.write("plan.json", normalized, replace=False)
            elif existing.authority_sha256() != normalized.authority_sha256():
                raise UpgradeConflict("Local journal belongs to different accepted authority")
            self.plan = existing or normalized
            self._entries()

    def _entries(self) -> tuple[UpgradeJournalEntry, ...]:
        from coire_core.models.upgrades import UpgradeJournalEntry, UpgradeReceiptAck

        keys: list[str] = []
        for path in self.store.root.iterdir():
            if path.name in {"journal.lock", "plan.json", "ack.json"}:
                continue
            if re.fullmatch(r"receipt-[0-9]{20}\.json", path.name) is None:
                raise UpgradeUnavailable("Upgrade journal contains unreconciled data")
            keys.append(path.name)
        if len(keys) > 4096:
            raise UpgradeUnavailable("Upgrade journal exceeds its receipt bound")
        entries: list[UpgradeJournalEntry] = []
        previous: str | None = None
        operation = self.plan.operation
        for sequence, key in enumerate(sorted(keys), start=1):
            entry = self.store.read(key, UpgradeJournalEntry)
            if entry is None or (
                entry.receipt.job_id,
                entry.receipt.generation,
                entry.receipt.sequence,
                entry.previous_sha256,
                key,
            ) != (operation.job_id, operation.generation, sequence, previous, self.key(sequence)):
                raise UpgradeUnavailable("Upgrade journal receipt chain differs")
            entries.append(entry)
            previous = entry.sha256()
        acknowledged = self.store.read("ack.json", UpgradeReceiptAck)
        if acknowledged is not None and (
            (acknowledged.job_id, acknowledged.generation)
            != (operation.job_id, operation.generation)
            or acknowledged.sequence > len(entries)
        ):
            raise UpgradeUnavailable(
                "Upgrade journal acknowledgment differs from retained evidence"
            )
        return tuple(entries)

    @staticmethod
    def key(sequence: int) -> str:
        if not 1 <= sequence <= 2**63 - 1:
            raise UpgradeConflict("Receipt sequence is out of bounds")
        return f"receipt-{sequence:020d}.json"

    def append(self, receipt: UpgradeReceipt, *, resource_identity: str | None = None) -> bool:
        from coire_core.models.upgrades import UpgradeJournalEntry, UpgradeReceipt

        receipt = UpgradeReceipt.model_validate(receipt.model_dump(mode="json"))
        operation = self.plan.operation
        if (receipt.job_id, receipt.generation) != (operation.job_id, operation.generation):
            raise UpgradeConflict("Receipt belongs to another local owner")
        with self.store.ownership("journal.lock", wait_s=1):
            entries = self._entries()
            if receipt.sequence <= len(entries):
                existing = entries[receipt.sequence - 1]
                if (
                    existing.receipt.sha256() != receipt.sha256()
                    or existing.resource_identity != resource_identity
                ):
                    raise UpgradeConflict("Receipt replay contains different evidence")
                return False
            if receipt.sequence != len(entries) + 1:
                raise UpgradeConflict("Local receipt sequence is not contiguous")
            entry = UpgradeJournalEntry(
                receipt=receipt,
                previous_sha256=entries[-1].sha256() if entries else None,
                resource_identity=resource_identity,
            )
            self.store.write(self.key(receipt.sequence), entry, replace=False)
            return True

    def page(self, after_sequence: int = 0) -> tuple[UpgradeReceipt, ...]:
        if after_sequence < 0:
            raise UpgradeConflict("Receipt replay cursor is invalid")
        with self.store.ownership("journal.lock", wait_s=1):
            return tuple(
                entry.receipt for entry in self._entries()[after_sequence : after_sequence + 32]
            )

    def latest(self) -> UpgradeReceipt | None:
        with self.store.ownership("journal.lock", wait_s=1):
            entries = self._entries()
            return entries[-1].receipt if entries else None

    def acknowledged_sequence(self) -> int:
        from coire_core.models.upgrades import UpgradeReceiptAck

        with self.store.ownership("journal.lock", wait_s=1):
            self._entries()
            ack = self.store.read("ack.json", UpgradeReceiptAck)
            return ack.sequence if ack is not None else 0

    def acknowledge(self, ack: UpgradeReceiptAck) -> None:
        from coire_core.models.upgrades import UpgradeReceiptAck

        operation = self.plan.operation
        if (ack.job_id, ack.generation) != (operation.job_id, operation.generation):
            raise UpgradeConflict("Durable acknowledgment belongs to another owner")
        with self.store.ownership("journal.lock", wait_s=1):
            entries = self._entries()
            previous = self.store.read("ack.json", UpgradeReceiptAck)
            if ack.sequence > len(entries) or (
                previous is not None and ack.sequence < previous.sequence
            ):
                raise UpgradeConflict("Durable receipt acknowledgment differs")
            self.store.write("ack.json", ack)
            # Receipt bytes remain retained; only an explicit terminal retention policy
            # may prune them after independent recovery/source references are reconciled.
