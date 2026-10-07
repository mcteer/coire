"""Shared-lock, immutable-intent and per-filesystem crash/release accounting."""

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coire_core.models.acquisition import ReservationRequest, ReservationState
from coire_core.settings import Settings
from coire_node.reservations import (
    ReservationConflict,
    ReservationLedger,
    ReservationLedgerUnavailable,
    ReservationRefused,
)
from coire_node.store import Store, write_atomic


def request(memory: int = 1, disk: int = 1) -> ReservationRequest:
    return ReservationRequest(
        idempotency_key=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        memory_bytes=memory,
        disk_bytes=disk,
    )


def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReservationLedger:
    store = Store(tmp_path)
    monkeypatch.setattr(store, "free_bytes", lambda: 100)
    monkeypatch.setattr(
        "coire_node.reservations.shutil.disk_usage", lambda _path: SimpleNamespace(free=100)
    )
    monkeypatch.setattr(
        "coire_node.reservations.psutil.virtual_memory", lambda: SimpleNamespace(total=1000)
    )
    return ReservationLedger(
        Settings(node_state_dir=str(tmp_path / "state")),
        store,
        lambda: 0,
        memory_lock=threading.RLock(),
    )


@pytest.mark.parametrize("field", ["workflow_id", "variant_id", "memory_bytes", "disk_bytes"])
def test_duplicate_key_compares_every_payload_field_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    manager = ledger(tmp_path, monkeypatch)
    intent = request()
    manager.hold(intent)
    recovered = ReservationLedger(manager.settings, manager.store, lambda: 0)
    changed = intent.model_copy(update={field: 2 if field.endswith("bytes") else uuid.uuid4()})
    with pytest.raises(ReservationConflict, match="immutable payload"):
        recovered.hold(changed)
    assert recovered.held_bytes() == 1 and recovered.held_disk_bytes() == 1
    assert recovered.hold(intent)[1] is False


def test_returned_request_and_receipt_cannot_mutate_durable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ledger(tmp_path, monkeypatch)
    intent = request()
    receipt, _created = manager.hold(intent)
    receipt.memory_bytes = 99
    intent.memory_bytes = 99
    assert manager.held_bytes() == 1
    recovered = manager.get(receipt.id)
    assert recovered is not None and recovered.memory_bytes == 1


def test_concurrent_pending_disk_holds_cannot_overcommit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ledger(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)

    def admit(_index: int) -> bool:
        barrier.wait(timeout=2)
        try:
            manager.hold(request(disk=60))
            return True
        except ReservationRefused:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(admit, range(2))) == [False, True]
    assert manager.held_disk_bytes() == 60


def test_filesystem_scope_floor_and_changed_mount_are_conservative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ledger(tmp_path, monkeypatch)
    cache = tmp_path / "cache"
    cache.mkdir()
    original_stat = Path.stat
    device = [987654]

    def stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        result = original_stat(path, *args, **kwargs)
        if path == cache:
            values = list(result)
            values[2] = device[0]
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", stat)
    manager.hold(request(disk=60))
    cache_intent = request(disk=60)
    manager.hold(cache_intent, disk_path=cache, disk_floor_bytes=40)
    assert manager.held_disk_bytes() == 60 and manager.held_disk_bytes(cache) == 60
    with pytest.raises(ReservationRefused):
        manager.hold(request(disk=1), disk_path=cache)
    with pytest.raises(ReservationConflict):
        manager.hold(cache_intent, disk_path=cache, disk_floor_bytes=0)
    device[0] += 1
    assert manager.held_disk_bytes() == 120


def test_legacy_unknown_and_expired_scopes_count_until_explicit_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ledger(tmp_path, monkeypatch)
    identity = uuid.uuid4()
    manager._path.parent.mkdir()
    write_atomic(
        manager._path,
        json.dumps(
            {
                str(identity): {
                    "id": str(identity),
                    "state": "expired",
                    "memory_bytes": 80,
                    "disk_bytes": 80,
                    "occupants": [],
                }
            }
        ).encode(),
    )
    recovered = ReservationLedger(manager.settings, manager.store, lambda: 0)
    assert recovered.held_bytes() == 80 and recovered.held_disk_bytes() == 80
    with pytest.raises(ReservationRefused):
        recovered.hold(request(disk=21))
    with pytest.raises(ReservationConflict):
        recovered.hold(
            request().model_copy(
                update={"idempotency_key": identity, "memory_bytes": 80, "disk_bytes": 80}
            )
        )
    assert recovered.release(identity)
    assert recovered.held_bytes() == recovered.held_disk_bytes() == 0


def test_protected_scope_restart_requires_stop_guard_and_counts_peak_rss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ledger(tmp_path, monkeypatch)
    intent = request(memory=60, disk=60)
    manager.hold(intent, disk_path=tmp_path, require_stop=True)
    manager.bind_owner(
        intent.idempotency_key, release_check=lambda: False, footprint_bytes=lambda: 90
    )
    assert manager.held_bytes() == 90
    assert not manager.release(intent.idempotency_key)
    recovered = ReservationLedger(manager.settings, manager.store, lambda: 0)
    assert not recovered.release(intent.idempotency_key)
    assert recovered.held_bytes() == recovered.held_disk_bytes() == 60
    recovered.bind_owner(
        intent.idempotency_key, release_check=lambda: False, footprint_bytes=lambda: None
    )
    assert recovered.held_bytes() == 60
    recovered.bind_owner(
        intent.idempotency_key, release_check=lambda: True, footprint_bytes=lambda: 0
    )
    assert recovered.release(intent.idempotency_key)
    assert recovered.held_bytes() == recovered.held_disk_bytes() == 0
    receipt = recovered.get(intent.idempotency_key)
    assert receipt is not None and receipt.state is ReservationState.RELEASED


def test_persistence_failure_latches_admission_and_release_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ledger(tmp_path, monkeypatch)
    intent = request()

    def fail(*_args: object) -> None:
        raise OSError("journal unavailable")

    monkeypatch.setattr("coire_node.reservations.write_atomic", fail)
    with pytest.raises(ReservationLedgerUnavailable):
        manager.hold(intent)
    with pytest.raises(ReservationLedgerUnavailable):
        manager.held_bytes()
    with pytest.raises(ReservationLedgerUnavailable):
        manager.release(intent.idempotency_key)


def test_duplicate_json_scope_cannot_hide_a_live_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ledger(tmp_path, monkeypatch)
    identity = uuid.uuid4()
    manager._path.parent.mkdir()
    held = json.dumps(
        {
            "id": str(identity),
            "state": "held",
            "memory_bytes": 80,
            "disk_bytes": 80,
            "occupants": [],
        }
    )
    released = held.replace('"held"', '"released"')
    write_atomic(
        manager._path,
        (
            '{"' + str(identity) + '":' + held + ',"' + str(identity) + '":' + released + "}"
        ).encode(),
    )
    with pytest.raises(ReservationLedgerUnavailable):
        ReservationLedger(manager.settings, manager.store, lambda: 0)


def test_separate_trainer_disk_envelope_joins_same_filesystem_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = ledger(tmp_path, monkeypatch)
    external = [80]
    manager = ReservationLedger(
        base.settings, base.store, lambda: 0, additional_held_disk_bytes=lambda _path: external[0]
    )
    assert manager.held_disk_bytes() == 80
    with pytest.raises(ReservationRefused):
        manager.hold(request(disk=21))
    manager.hold(request(disk=20))
    assert manager.held_disk_bytes() == 100
    assert manager.held_disk_bytes(include_external=False) == 20
    external[0] = -1
    with pytest.raises(ReservationLedgerUnavailable):
        manager.hold(request())
