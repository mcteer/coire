from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import pytest

from coire_core.models.acquisition import ReservationRequest, ReservationState
from coire_core.settings import Settings
from coire_node.reservations import ReservationLedger, ReservationRefused
from coire_node.store import Store


def _request(memory: int = 1, disk: int = 1) -> ReservationRequest:
    return ReservationRequest(
        idempotency_key=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        memory_bytes=memory,
        disk_bytes=disk,
    )


def test_hold_is_persistent_idempotent_and_release_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(node_state_dir=str(tmp_path / "state"), node_store_dir=str(tmp_path))
    store = Store(tmp_path)
    request = _request()
    ledger = ReservationLedger(settings, store, lambda: 0)
    first, created = ledger.hold(request)
    second, created_again = ledger.hold(request)
    assert created and not created_again and first == second
    assert ReservationLedger(settings, store, lambda: 0).get(first.id) == first
    assert ledger.release(first.id)
    assert ledger.release(first.id)
    assert ledger.get(first.id).state is ReservationState.RELEASED  # type: ignore[union-attr]


def test_impossible_memory_is_distinct_from_busy_capacity(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        node_state_dir=str(tmp_path / "state"),
        node_store_dir=str(tmp_path),
        node_memory_budget_fraction=0.5,
    )
    store = Store(tmp_path)
    monkeypatch.setattr("psutil.virtual_memory", lambda: type("VM", (), {"total": 100})())
    ledger = ReservationLedger(settings, store, lambda: 40)
    with pytest.raises(ReservationRefused, match="needs 60") as impossible:
        ledger.hold(_request(memory=60))
    assert impossible.value.impossible
    with pytest.raises(ReservationRefused, match="needs 20") as busy:
        ledger.hold(_request(memory=20))
    assert not busy.value.impossible


def test_acquisition_hold_waits_for_shared_engine_and_image_memory_lock(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(node_state_dir=str(tmp_path / "state"), node_store_dir=str(tmp_path))
    shared = threading.RLock()
    ledger = ReservationLedger(settings, Store(tmp_path), lambda: 0, memory_lock=shared)
    attempted = threading.Event()

    def hold() -> bool:
        attempted.set()
        return ledger.hold(_request())[1]

    with ThreadPoolExecutor(max_workers=1) as pool:
        with shared:
            future = pool.submit(hold)
            assert attempted.wait(timeout=2)
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.1)
        assert future.result(timeout=2)
