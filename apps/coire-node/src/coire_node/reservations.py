"""Crash-safe conversion reservations sharing the engine memory budget."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from coire_core.models.acquisition import (
    Reservation,
    ReservationRequest,
    ReservationState,
)
from coire_core.settings import Settings
from coire_node.store import Store, write_atomic


class ReservationRefused(RuntimeError):
    def __init__(self, *, impossible: bool, required: int, committed: int, budget: int) -> None:
        self.impossible = impossible
        self.required = required
        self.committed = committed
        self.budget = budget
        super().__init__(f"needs {required} bytes; {committed} of {budget} already committed")


class ReservationLedger:
    def __init__(
        self, settings: Settings, store: Store, committed_engine_bytes: Callable[[], int]
    ) -> None:
        self.settings = settings
        self.store = store
        self._committed_engine_bytes = committed_engine_bytes
        self._path = Path(settings.node_state_dir) / "reservations.json"
        self._lock = threading.RLock()
        self._items = self._load()

    def _load(self) -> dict[uuid.UUID, Reservation]:
        try:
            values = json.loads(self._path.read_text())
            return {
                uuid.UUID(key): Reservation.model_validate(value) for key, value in values.items()
            }
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(
            self._path,
            json.dumps(
                {str(key): value.model_dump(mode="json") for key, value in self._items.items()},
                indent=2,
            ).encode(),
        )

    def held_bytes(self) -> int:
        return sum(
            item.memory_bytes
            for item in self._items.values()
            if item.state is ReservationState.HELD
        )

    def hold(self, request: ReservationRequest) -> tuple[Reservation, bool]:
        with self._lock:
            existing = self._items.get(request.idempotency_key)
            if existing is not None:
                return existing, False
            budget = int(
                self.settings.node_memory_budget_fraction
                * __import__("psutil").virtual_memory().total
            )
            engines = int(self._committed_engine_bytes())
            committed = engines + self.held_bytes()
            disk_free = self.store.free_bytes()
            if request.memory_bytes > budget:
                raise ReservationRefused(
                    impossible=True,
                    required=request.memory_bytes,
                    committed=committed,
                    budget=budget,
                )
            if committed + request.memory_bytes > budget or request.disk_bytes > disk_free:
                raise ReservationRefused(
                    impossible=False,
                    required=request.memory_bytes,
                    committed=committed,
                    budget=budget,
                )
            item = Reservation(
                id=request.idempotency_key,
                state=ReservationState.HELD,
                memory_bytes=request.memory_bytes,
                disk_bytes=request.disk_bytes,
                occupants=[f"engines:{engines}"] if engines else [],
            )
            self._items[item.id] = item
            self._save()
            return item, True

    def release(self, reservation_id: uuid.UUID) -> bool:
        with self._lock:
            item = self._items.get(reservation_id)
            if item is None:
                return False
            if item.state is ReservationState.HELD:
                item.state = ReservationState.RELEASED
                self._save()
            return True

    def get(self, reservation_id: uuid.UUID) -> Reservation | None:
        return self._items.get(reservation_id)
