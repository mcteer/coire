"""Crash-safe shared memory admission and filesystem-scoped aggregate disk holds."""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from coire_core.models.acquisition import (
    Reservation,
    ReservationRequest,
    ReservationState,
)
from coire_core.settings import Settings
from coire_node.store import Store, write_atomic

if TYPE_CHECKING:
    from coire_node.training.journal import TrainingJournal

_MAX_JOURNAL_BYTES = 16 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate reservation journal key")
        value[key] = item
    return value


class ReservationRefused(RuntimeError):
    def __init__(self, *, impossible: bool, required: int, committed: int, budget: int) -> None:
        self.impossible = impossible
        self.required = required
        self.committed = committed
        self.budget = budget
        super().__init__(f"needs {required} bytes; {committed} of {budget} already committed")


class ReservationLedgerUnavailable(RuntimeError):
    """An existing hold journal cannot be trusted for memory admission."""


class ReservationConflict(ReservationRefused):
    """An idempotency key cannot change its immutable intent or local disk scope."""

    def __init__(self, request: ReservationRequest, existing: Reservation) -> None:
        super().__init__(
            impossible=False,
            required=request.memory_bytes,
            committed=existing.memory_bytes,
            budget=existing.memory_bytes,
        )
        self.args = ("reservation identity conflicts with its immutable payload",)


class ReservationLedger:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        committed_engine_bytes: Callable[[], int],
        *,
        memory_lock: threading.RLock | None = None,
        additional_held_disk_bytes: Callable[[Path], int] | None = None,
        disk_quota_bytes: int | None = None,
        disk_usage_bytes: Callable[[], int] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self._committed_engine_bytes = committed_engine_bytes
        # A separately journaled trainer supplies its full/unknown disk envelope
        # for this filesystem under the same admission lock. No caller path or
        # estimate is accepted through the reservation wire request.
        self._additional_held_disk_bytes = additional_held_disk_bytes
        self._disk_quota_bytes = disk_quota_bytes
        self._disk_usage_bytes = disk_usage_bytes
        self._training_quota_root = (Path(settings.node_state_dir) / "training").resolve()
        self._path = Path(settings.node_state_dir) / "reservations.json"
        self._lock = memory_lock or threading.RLock()
        self._metadata: dict[uuid.UUID, dict[str, Any]] = {}
        self._owners: dict[uuid.UUID, tuple[Callable[[], bool], Callable[[], int | None]]] = {}
        self._unavailable = False
        self._items = self._load()

    def _load(self) -> dict[uuid.UUID, Reservation]:
        fd = -1
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ReservationLedgerUnavailable("reservation journal needs recovery") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > _MAX_JOURNAL_BYTES
            ):
                raise ReservationLedgerUnavailable("reservation journal needs recovery")
            with os.fdopen(fd, "r", closefd=False) as source:
                raw = source.read(_MAX_JOURNAL_BYTES + 1)
            if len(raw) > _MAX_JOURNAL_BYTES:
                raise ReservationLedgerUnavailable("reservation journal needs recovery")
            values = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(values, dict):
                raise ValueError("reservation journal must be an object")
            if values.get("schema_version") == 2:
                if set(values) != {"schema_version", "items"} or not isinstance(
                    values["items"], dict
                ):
                    raise ValueError("reservation journal envelope invalid")
                items: dict[uuid.UUID, Reservation] = {}
                for key, record in values["items"].items():
                    identity = uuid.UUID(key)
                    if not isinstance(record, dict) or set(record) - {
                        "disk_credit_bytes",
                        "materialized_paths",
                        "owner_kind",
                    } != {
                        "reservation",
                        "request",
                        "disk_root",
                        "disk_device",
                        "disk_floor_bytes",
                        "require_stop",
                    }:
                        raise ValueError("reservation journal scope invalid")
                    item = Reservation.model_validate(record["reservation"])
                    credit = record.get("disk_credit_bytes", 0)
                    paths = record.get("materialized_paths", [])
                    if (
                        type(credit) is not int
                        or not 0 <= credit <= item.disk_bytes
                        or not isinstance(paths, list)
                        or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths)
                    ):
                        raise ValueError("reservation disk coverage invalid")
                    if record.get("owner_kind") not in {None, "artifact-import"}:
                        raise ValueError("reservation owner kind invalid")
                    request = (
                        ReservationRequest.model_validate(record["request"])
                        if record["request"] is not None
                        else None
                    )
                    if item.id != identity or (
                        request is not None
                        and (
                            request.idempotency_key != identity
                            or request.memory_bytes != item.memory_bytes
                            or request.disk_bytes != item.disk_bytes
                        )
                    ):
                        raise ValueError("reservation journal identity invalid")
                    if (
                        (
                            record["disk_root"] is not None
                            and (
                                not isinstance(record["disk_root"], str)
                                or not Path(record["disk_root"]).is_absolute()
                            )
                        )
                        or (
                            record["disk_device"] is not None
                            and (
                                type(record["disk_device"]) is not int or record["disk_device"] < 0
                            )
                        )
                        or type(record["disk_floor_bytes"]) is not int
                        or record["disk_floor_bytes"] < 0
                        or type(record["require_stop"]) is not bool
                    ):
                        raise ValueError("reservation journal accounting invalid")
                    if (record["disk_root"] is None) != (record["disk_device"] is None):
                        raise ValueError("reservation journal disk scope incomplete")
                    items[identity] = item
                    self._metadata[identity] = {
                        name: value for name, value in record.items() if name != "reservation"
                    }
                    self._metadata[identity].update(
                        disk_credit_bytes=credit,
                        materialized_paths=paths,
                        owner_kind=record.get("owner_kind"),
                    )
                return items
            # Legacy holds have no proof of filesystem or request identity. Count
            # them on every filesystem and never accept an unverifiable replay.
            items = {}
            for key, value in values.items():
                identity = uuid.UUID(key)
                item = Reservation.model_validate(value)
                if item.id != identity:
                    raise ValueError("legacy reservation identity invalid")
                items[identity] = item
                self._metadata[identity] = {
                    "request": None,
                    "disk_root": None,
                    "disk_device": None,
                    "disk_floor_bytes": 0,
                    "require_stop": False,
                    "disk_credit_bytes": 0,
                    "materialized_paths": [],
                    "owner_kind": None,
                }
            return items
        except (OSError, ValueError, TypeError) as exc:
            raise ReservationLedgerUnavailable("reservation journal needs recovery") from exc
        finally:
            os.close(fd)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.dumps(
                {
                    "schema_version": 2,
                    "items": {
                        str(key): {
                            "reservation": value.model_dump(mode="json"),
                            **self._metadata[key],
                        }
                        for key, value in self._items.items()
                    },
                },
                sort_keys=True,
            ).encode()
            if len(data) > _MAX_JOURNAL_BYTES:
                raise ValueError("reservation journal capacity exceeded")
            write_atomic(self._path, data)
            fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, ValueError) as exc:
            # A failure may follow the rename. Refuse admission/release rather
            # than guessing which snapshot survived or advertising free capacity.
            self._unavailable = True
            raise ReservationLedgerUnavailable("reservation journal needs recovery") from exc

    def _check_available(self) -> None:
        if self._unavailable:
            raise ReservationLedgerUnavailable("reservation journal needs recovery")

    def bind_owner(
        self,
        reservation_id: uuid.UUID,
        *,
        release_check: Callable[[], bool],
        footprint_bytes: Callable[[], int | None],
    ) -> None:
        """Node-owned runtime guards; not supplied by callers or persisted as code.

        After restart a protected scope without its owner retains both holds.
        Legacy owner scopes are upgraded to protected before serving commands.
        """
        with self._lock:
            self._check_available()
            self._owners[reservation_id] = (release_check, footprint_bytes)
            if reservation_id in self._items and not self._metadata[reservation_id]["require_stop"]:
                self._metadata[reservation_id]["require_stop"] = True
                self._save()

    def _device(self, metadata: dict[str, Any]) -> int | None:
        root = metadata["disk_root"]
        if root is None:
            return None
        try:
            path = Path(root)
            if path.is_symlink() or path.stat().st_dev != metadata["disk_device"]:
                return None
        except OSError:
            return None
        return int(metadata["disk_device"])

    def held_disk_bytes(
        self, disk_path: Path | None = None, *, include_external: bool = True
    ) -> int:
        """Count unreleased scopes. Trainers exclude their own external callback
        when calculating availability for a journal which already counts itself.
        """
        with self._lock:
            self._check_available()
            device = (disk_path or self.store.root).stat().st_dev
            held = sum(
                item.disk_bytes - int(self._metadata[identity].get("disk_credit_bytes", 0))
                for identity, item in self._items.items()
                if item.state is not ReservationState.RELEASED
                and self._device(self._metadata[identity]) in {None, device}
            )
            if include_external and self._additional_held_disk_bytes is not None:
                try:
                    other = self._additional_held_disk_bytes(disk_path or self.store.root)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise ReservationLedgerUnavailable("external disk holds need recovery") from exc
                if type(other) is not int or other < 0:
                    raise ReservationLedgerUnavailable("external disk holds need recovery")
                held += other
            return held

    def held_bytes(self) -> int:
        with self._lock:
            self._check_available()
            total = 0
            for identity, item in self._items.items():
                if item.state is ReservationState.RELEASED:
                    continue
                measured = 0
                owner = self._owners.get(identity)
                if owner is not None:
                    try:
                        value = owner[1]()
                        if type(value) is int and value >= 0:
                            measured = value
                    except (OSError, ValueError, RuntimeError, psutil.Error):
                        pass  # Unknown ownership/measurement never frees its bound.
                total += max(item.memory_bytes, measured)
            return total

    def hold(
        self,
        request: ReservationRequest,
        *,
        disk_path: Path | None = None,
        disk_floor_bytes: int = 0,
        require_stop: bool = False,
        disk_credit_bytes: int = 0,
        materialized_paths: tuple[Path, ...] = (),
        owner_kind: str | None = None,
    ) -> tuple[Reservation, bool]:
        with self._lock:
            self._check_available()
            request = ReservationRequest.model_validate(request.model_dump(mode="json"))
            path = disk_path if disk_path is not None else self.store.root
            if (
                not path.is_absolute()
                or path.is_symlink()
                or not path.is_dir()
                or type(disk_floor_bytes) is not int
                or disk_floor_bytes < 0
                or type(require_stop) is not bool
                or type(disk_credit_bytes) is not int
                or not 0 <= disk_credit_bytes <= request.disk_bytes
                or any(not p.is_absolute() for p in materialized_paths)
                or owner_kind not in {None, "artifact-import"}
            ):
                raise ValueError("reservation disk scope is unsafe")
            path = path.resolve(strict=True)
            device = path.stat().st_dev
            metadata = {
                "request": request.model_dump(mode="json"),
                "disk_root": str(path),
                "disk_device": device,
                "disk_floor_bytes": disk_floor_bytes,
                "require_stop": require_stop,
                "disk_credit_bytes": disk_credit_bytes,
                "materialized_paths": [str(p) for p in materialized_paths],
                "owner_kind": owner_kind,
            }
            existing = self._items.get(request.idempotency_key)
            if existing is not None:
                if self._metadata[existing.id] != metadata:
                    raise ReservationConflict(request, existing)
                return existing.model_copy(deep=True), False
            budget = int(self.settings.node_memory_budget_fraction * psutil.virtual_memory().total)
            engines = int(self._committed_engine_bytes())
            committed = engines + self.held_bytes()
            disk_free = (
                self.store.free_bytes() if disk_path is None else shutil.disk_usage(path).free
            )
            disk_held = self.held_disk_bytes(path)
            floor = max(
                [
                    disk_floor_bytes,
                    *(
                        self._metadata[identity]["disk_floor_bytes"]
                        for identity, item in self._items.items()
                        if item.state is not ReservationState.RELEASED
                        and self._device(self._metadata[identity]) in {None, device}
                    ),
                ]
            )
            if request.memory_bytes > budget:
                raise ReservationRefused(
                    impossible=True,
                    required=request.memory_bytes,
                    committed=committed,
                    budget=budget,
                )
            if committed + request.memory_bytes > budget:
                raise ReservationRefused(
                    impossible=False,
                    required=request.memory_bytes,
                    committed=committed,
                    budget=budget,
                )
            effective_disk = request.disk_bytes - disk_credit_bytes
            if (
                self._disk_quota_bytes is not None
                and self._disk_usage_bytes is not None
                and path.is_relative_to(self._training_quota_root)
            ):
                usage = self._disk_usage_bytes()
                if type(usage) is not int or usage < 0:
                    raise ReservationLedgerUnavailable("artifact quota observation unavailable")
                if usage + effective_disk > self._disk_quota_bytes:
                    raise ReservationRefused(
                        impossible=False,
                        required=effective_disk,
                        committed=usage,
                        budget=self._disk_quota_bytes,
                    )
            if disk_held + effective_disk + floor > disk_free:
                raise ReservationRefused(
                    impossible=False,
                    required=request.disk_bytes,
                    committed=disk_held,
                    budget=max(0, disk_free - floor),
                )
            item = Reservation(
                id=request.idempotency_key,
                state=ReservationState.HELD,
                memory_bytes=request.memory_bytes,
                disk_bytes=request.disk_bytes,
                occupants=[f"engines:{engines}"] if engines else [],
            )
            self._items[item.id] = item
            self._metadata[item.id] = metadata
            self._save()
            return item.model_copy(deep=True), True

    def disk_scopes(
        self, root: Path | None = None
    ) -> list[tuple[int, tuple[Path, ...], str | None, int]]:
        """Private quota projection: each held envelope owns its materialized bytes once."""
        with self._lock:
            self._check_available()
            return [
                (
                    item.disk_bytes - self._metadata[identity].get("disk_credit_bytes", 0),
                    tuple(Path(p) for p in self._metadata[identity].get("materialized_paths", [])),
                    (self._metadata[identity].get("request") or {}).get("workflow_id"),
                    int(self._metadata[identity].get("disk_credit_bytes", 0)),
                )
                for identity, item in self._items.items()
                if item.state is not ReservationState.RELEASED
                and (
                    root is None
                    or self._metadata[identity]["disk_root"] is None
                    or Path(self._metadata[identity]["disk_root"]).is_relative_to(root)
                )
            ]

    def owner_scopes(self, owner_kind: str) -> list[tuple[uuid.UUID, uuid.UUID]]:
        with self._lock:
            self._check_available()
            return [
                (identity, uuid.UUID(self._metadata[identity]["request"]["workflow_id"]))
                for identity, item in self._items.items()
                if item.state is not ReservationState.RELEASED
                and self._metadata[identity].get("owner_kind") == owner_kind
            ]

    def release(self, reservation_id: uuid.UUID) -> bool:
        with self._lock:
            self._check_available()
            item = self._items.get(reservation_id)
            if item is None:
                return False
            if item.state is not ReservationState.RELEASED:
                if self._metadata[reservation_id]["require_stop"]:
                    owner = self._owners.get(reservation_id)
                    if owner is None or owner[0]() is not True:
                        return False
                item.state = ReservationState.RELEASED
                self._save()
            return True

    def get(self, reservation_id: uuid.UUID) -> Reservation | None:
        with self._lock:
            self._check_available()
            item = self._items.get(reservation_id)
            return item.model_copy(deep=True) if item is not None else None


class TrainingDiskBudget:
    """Aggregate retained bytes and disjoint envelopes, including materialized bytes once.

    All callers share the node admission RLock. Paths are generated local ownership
    scopes; symlinks or corrupt manifests make quota observation unavailable.
    """

    def __init__(self, root: Path, lock: threading.RLock, quota: int) -> None:
        self.root = root.resolve()
        self.lock = lock
        self.quota = quota
        self.journals: list[TrainingJournal] = []
        self.ledger: ReservationLedger | None = None

    def committed_bytes(self) -> int:
        with self.lock:
            files: dict[Path, int] = {}
            if self.root.exists():
                for path in self.root.rglob("*"):
                    if path.is_symlink():
                        raise ReservationLedgerUnavailable("artifact quota path is linked")
                    if path.is_file():
                        files[path] = path.stat().st_size
            remaining = dict(files)
            files_by_scope: dict[Path, set[Path]] = {}
            for path in files:
                for scope in (path, *path.parents):
                    files_by_scope.setdefault(scope, set()).add(path)

            def claim(paths: tuple[Path, ...], bound: int) -> int:
                owned = {
                    path
                    for scope in paths
                    for path in files_by_scope.get(scope, ())
                    if path in remaining
                }
                materialized = sum(remaining.pop(p) for p in owned)
                return max(bound, materialized)

            total = 0
            artifact_owners: dict[str, list[Path]] = {}
            from coire_core.models.training_node import (
                TrainingArtifactManifest,
                TrainingRankComponentManifest,
            )

            for path in files:
                if path.name == "component.json" and path.is_relative_to(self.root / "artifacts"):
                    if files[path] > 64 * 1024**2:
                        raise ReservationLedgerUnavailable("rank quota manifest is oversized")
                    component = TrainingRankComponentManifest.model_validate_json(path.read_bytes())
                    artifact_owners.setdefault(component.attempt_id, []).append(path.parent)
                if path.name == "manifest.json" and path.parent.parent == self.root / "artifacts":
                    if files[path] > 64 * 1024**2:
                        raise ReservationLedgerUnavailable("artifact quota manifest is oversized")
                    manifest = TrainingArtifactManifest.model_validate_json(path.read_bytes())
                    if manifest.attempt_id is not None:
                        artifact_owners.setdefault(manifest.attempt_id, []).append(path.parent)
            for journal in self.journals:
                for record in journal.records():
                    paths = (
                        journal.root / record["attempt_id"],
                        *artifact_owners.get(record["attempt_id"], []),
                    )
                    if journal.root == self.root / "measurements":
                        paths += (self.root / "probe-artifacts" / record["attempt_id"],)
                    total += claim(paths, record["disk_bytes"])
            if self.ledger is not None:
                for bound, paths, workflow, credit in self.ledger.disk_scopes(self.root):
                    extraction = self.root / "extractions" / f"{workflow}.json"
                    if not paths and extraction in files:
                        if files[extraction] > 64 * 1024**2:
                            raise ReservationLedgerUnavailable(
                                "extraction quota journal is oversized"
                            )
                        record = json.loads(extraction.read_bytes())
                        adapter = uuid.UUID(record["request"]["adapter_id"])
                        paths = (
                            self.root / "artifacts" / f".extract-{workflow}",
                            self.root / "artifacts" / str(adapter),
                        )
                    total += claim(paths, bound + credit) - credit
            return total + sum(remaining.values())

    def available_for(self, journal: TrainingJournal) -> int:
        with self.lock:
            own = journal.held_bytes()[1]
            return max(0, self.quota - self.committed_bytes() + own)
