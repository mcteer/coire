"""Uncertain preparation/PID ownership keeps local holds; no MLX subprocesses on core."""

import os
import signal
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psutil
import pytest

from coire_core.models.acquisition import ReservationRequest
from coire_core.settings import Settings
from coire_node.reservations import ReservationLedger
from coire_node.store import Store
from coire_node.training.analysis_supervisor import AnalysisSupervisor


class Ledger:
    def __init__(self) -> None:
        self.released: list[uuid.UUID] = []

    def bind_owner(self, *_args: object, **_kwargs: object) -> None:
        pass

    def release(self, identity: uuid.UUID) -> bool:
        self.released.append(identity)
        return True


class OwnedProcess:
    pid = 42
    alive = True
    birth = 1.0

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv

    def cmdline(self) -> list[str]:
        return self.argv

    def create_time(self) -> float:
        return self.birth

    def uids(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(real=os.getuid())

    def status(self) -> str:
        return str(psutil.STATUS_RUNNING)

    def wait(self, timeout: int) -> None:
        self.alive = False

    def memory_info(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(rss=1)


def owned_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AnalysisSupervisor, Ledger, uuid.UUID, OwnedProcess]:
    ledger = Ledger()
    supervisor = AnalysisSupervisor(
        Settings(node_state_dir=str(tmp_path)), cast(ReservationLedger, ledger)
    )
    identity, nonce = uuid.uuid4(), str(uuid.uuid4())
    supervisor.path(identity).mkdir(mode=0o700)
    supervisor.save(
        identity,
        {
            "analysis_id": str(identity),
            "reservation_id": str(uuid.uuid4()),
            "state": "running",
            "pid": 42,
            "process_create_time": 1.0,
            "spawn_nonce": nonce,
        },
    )
    process = OwnedProcess(supervisor.argv(identity, nonce))

    def lookup(_pid: int) -> OwnedProcess:
        if not process.alive:
            raise psutil.NoSuchProcess(42)
        return process

    monkeypatch.setattr("coire_node.training.analysis_supervisor.psutil.Process", lookup)
    monkeypatch.setattr("coire_node.training.analysis_supervisor.os.getpgid", lambda pid: pid)
    return supervisor, ledger, identity, process


async def test_healthy_cancel_proves_death_cleans_private_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, ledger, identity, _process = owned_setup(tmp_path, monkeypatch)
    source = supervisor.path(identity) / "source.jsonl"
    source.write_bytes(b"private")
    signals: list[int] = []
    monkeypatch.setattr(
        "coire_node.training.analysis_supervisor.os.killpg",
        lambda _pid, value: signals.append(value),
    )
    observed = await supervisor.cancel(identity)
    assert observed.state == "cancelled" and signals == [signal.SIGTERM]
    assert len(ledger.released) == 1 and not source.exists()


async def test_recycled_pid_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, ledger, identity, process = owned_setup(tmp_path, monkeypatch)
    process.birth = 2.0
    monkeypatch.setattr(
        "coire_node.training.analysis_supervisor.os.killpg",
        lambda *_args: pytest.fail("recycled PID signalled"),
    )
    assert (await supervisor.cancel(identity)).state == "cancelled"
    assert len(ledger.released) == 1


async def test_changed_live_argv_retains_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, ledger, identity, process = owned_setup(tmp_path, monkeypatch)
    process.argv = [sys.executable, "unrelated", str(identity)]
    with pytest.raises(ValueError, match="marker"):
        await supervisor.cancel(identity)
    assert ledger.released == []


async def test_half_spawn_re_adopts_exact_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, ledger, identity, process = owned_setup(tmp_path, monkeypatch)
    journal = supervisor.journal(identity)
    journal.update(state="launching", pid=None, process_create_time=None)
    supervisor.save(identity, journal)
    monkeypatch.setattr(
        "coire_node.training.analysis_supervisor.psutil.process_iter", lambda: [process]
    )
    assert (await supervisor.status(identity)).state == "running"
    assert supervisor.journal(identity)["pid"] == 42 and not ledger.released


async def test_expired_live_worker_is_stopped_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, ledger, identity, _process = owned_setup(tmp_path, monkeypatch)
    journal = supervisor.journal(identity)
    journal["deadline"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    supervisor.save(identity, journal)
    monkeypatch.setattr("coire_node.training.analysis_supervisor.os.killpg", lambda *_args: None)
    assert (await supervisor.status(identity)).state == "failed"
    assert len(ledger.released) == 1


async def test_memory_guard_stops_live_worker_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    supervisor, ledger, identity, process = owned_setup(tmp_path, monkeypatch)
    journal = supervisor.journal(identity)
    journal["memory_bytes"] = 1024**3
    supervisor.save(identity, journal)
    monkeypatch.setattr(process, "memory_info", lambda: SimpleNamespace(rss=1024**3 + 1))
    monkeypatch.setattr("coire_node.training.analysis_supervisor.os.killpg", lambda *_args: None)
    assert (await supervisor.status(identity)).state == "failed"
    assert not process.alive and len(ledger.released) == 1


async def test_surviving_group_member_prevents_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, ledger, identity, process = owned_setup(tmp_path, monkeypatch)
    process.alive = False
    member = OwnedProcess(["child"])
    member.pid = 43
    monkeypatch.setattr(
        "coire_node.training.analysis_supervisor.psutil.process_iter", lambda: [member]
    )
    monkeypatch.setattr("coire_node.training.analysis_supervisor.os.getpgid", lambda _pid: 42)
    with pytest.raises(ValueError, match="group death"):
        await supervisor.cancel(identity)
    assert not ledger.released


async def test_cleanup_failure_keeps_hold_and_retries_without_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, ledger, identity, _process = owned_setup(tmp_path, monkeypatch)
    cleanup = supervisor.cleanup

    def fail(_identity: uuid.UUID) -> None:
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(supervisor, "cleanup", fail)
    monkeypatch.setattr("coire_node.training.analysis_supervisor.os.killpg", lambda *_args: None)
    with pytest.raises(OSError, match="cleanup"):
        await supervisor.cancel(identity)
    assert not ledger.released and supervisor.journal(identity)["release_pending"]
    monkeypatch.setattr(supervisor, "cleanup", cleanup)
    assert (await supervisor.status(identity)).state == "cancelled"
    assert len(ledger.released) == 1


async def test_cancel_escalates_without_releasing_before_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, ledger, identity, process = owned_setup(tmp_path, monkeypatch)
    signals: list[int] = []

    def kill(_pid: int, value: int) -> None:
        assert not ledger.released
        signals.append(value)

    def wait(timeout: int) -> None:
        if signals[-1] == signal.SIGTERM:
            raise psutil.TimeoutExpired(timeout, pid=42)
        process.alive = False

    monkeypatch.setattr(process, "wait", wait)
    monkeypatch.setattr("coire_node.training.analysis_supervisor.os.killpg", kill)
    assert (await supervisor.cancel(identity)).state == "cancelled"
    assert signals == [signal.SIGTERM, signal.SIGKILL] and len(ledger.released) == 1


def real_reservation_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AnalysisSupervisor, ReservationLedger, uuid.UUID, uuid.UUID, OwnedProcess]:
    supervisor, _ledger, identity, process = owned_setup(tmp_path, monkeypatch)
    manager = ReservationLedger(supervisor.settings, Store(tmp_path), lambda: 0)
    supervisor.reservations = manager
    journal = supervisor.journal(identity)
    reservation_id = uuid.UUID(journal["reservation_id"])
    journal["owns_reservation"] = True
    supervisor.save(identity, journal)
    manager.hold(
        ReservationRequest(
            workflow_id=identity,
            variant_id=uuid.uuid4(),
            idempotency_key=reservation_id,
            memory_bytes=64,
            disk_bytes=32,
        ),
        disk_path=supervisor.path(identity),
        require_stop=True,
    )
    supervisor._bind_owner(identity, reservation_id)
    (supervisor.path(identity) / "source.jsonl").write_bytes(b"private")
    return supervisor, manager, identity, reservation_id, process


async def test_real_memory_and_disk_holds_release_only_after_checked_stop_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, manager, identity, reservation_id, _process = real_reservation_setup(
        tmp_path, monkeypatch
    )
    assert manager.held_bytes() == 64 and manager.held_disk_bytes() == 32
    assert not manager.release(reservation_id)
    monkeypatch.setattr("coire_node.training.analysis_supervisor.os.killpg", lambda *_args: None)
    assert (await supervisor.cancel(identity)).state == "cancelled"
    assert manager.held_bytes() == manager.held_disk_bytes() == 0
    assert not (supervisor.path(identity) / "source.jsonl").exists()


async def test_terminal_cleanup_restart_rebinds_owner_before_freeing_both_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, manager, identity, reservation_id, _process = real_reservation_setup(
        tmp_path, monkeypatch
    )

    def fail(_identity: uuid.UUID) -> None:
        raise OSError("private cache still occupied")

    monkeypatch.setattr(supervisor, "cleanup", fail)
    monkeypatch.setattr("coire_node.training.analysis_supervisor.os.killpg", lambda *_args: None)
    with pytest.raises(OSError):
        await supervisor.cancel(identity)
    recovered_ledger = ReservationLedger(manager.settings, manager.store, lambda: 0)
    assert recovered_ledger.held_bytes() == 64 and recovered_ledger.held_disk_bytes() == 32
    assert not recovered_ledger.release(reservation_id)
    recovered = AnalysisSupervisor(manager.settings, recovered_ledger)
    assert (await recovered.status(identity)).state == "cancelled"
    assert recovered_ledger.held_bytes() == recovered_ledger.held_disk_bytes() == 0


async def test_terminal_state_alone_cannot_delete_cache_or_release_live_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, manager, identity, reservation_id, _process = real_reservation_setup(
        tmp_path, monkeypatch
    )
    journal = supervisor.journal(identity)
    journal.update(state="cancelled", stop_proven=True, release_pending=True)
    supervisor.save(identity, journal)
    assert not manager.release(reservation_id)
    with pytest.raises(ValueError, match="stop proof changed"):
        await supervisor.status(identity)
    assert manager.held_bytes() == 64 and manager.held_disk_bytes() == 32
    assert (supervisor.path(identity) / "source.jsonl").exists()


def test_observed_worker_overage_joins_shared_memory_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from coire_node.reservations import ReservationRefused

    supervisor, manager, _identity, _reservation_id, process = real_reservation_setup(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(process, "memory_info", lambda: SimpleNamespace(rss=96))
    monkeypatch.setattr(
        "coire_node.reservations.psutil.virtual_memory", lambda: SimpleNamespace(total=1000)
    )
    assert manager.held_bytes() == 96
    budget = int(supervisor.settings.node_memory_budget_fraction * 1000)
    with pytest.raises(ReservationRefused):
        manager.hold(
            ReservationRequest(
                workflow_id=uuid.uuid4(),
                variant_id=uuid.uuid4(),
                idempotency_key=uuid.uuid4(),
                memory_bytes=budget - 95,
                disk_bytes=1,
            )
        )


@pytest.mark.parametrize("state", ["preparing", "launching"])
async def test_uncertain_half_start_does_not_release_or_blindly_respawn(
    tmp_path: Path, state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger()
    supervisor = AnalysisSupervisor(
        Settings(node_state_dir=str(tmp_path)), cast(ReservationLedger, ledger)
    )
    identity = uuid.uuid4()
    supervisor.path(identity).mkdir(mode=0o700)
    supervisor.save(
        identity,
        {
            "analysis_id": str(identity),
            "command_id": str(uuid.uuid4()),
            "reservation_id": str(uuid.uuid4()),
            "state": state,
            "pid": None,
            "process_create_time": None,
        },
    )
    monkeypatch.setattr(supervisor, "_owned_process", lambda *_args: None)
    observed = await supervisor.status(identity)
    assert observed.state == "queued" and ledger.released == []
    with pytest.raises(ValueError, match="uncertain"):
        await supervisor.cancel(identity)
    assert ledger.released == []


async def test_unreadable_process_identity_keeps_counted_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger()
    supervisor = AnalysisSupervisor(
        Settings(node_state_dir=str(tmp_path)), cast(ReservationLedger, ledger)
    )
    identity = uuid.uuid4()
    supervisor.path(identity).mkdir(mode=0o700)
    supervisor.save(
        identity,
        {
            "analysis_id": str(identity),
            "command_id": str(uuid.uuid4()),
            "reservation_id": str(uuid.uuid4()),
            "state": "running",
            "pid": 42,
            "process_create_time": 1.0,
        },
    )

    class Process:
        def cmdline(self) -> list[str]:
            raise psutil.AccessDenied(42)

    monkeypatch.setattr(
        "coire_node.training.analysis_supervisor.psutil.Process", lambda _pid: Process()
    )
    with pytest.raises(ValueError, match="cannot be proved"):
        await supervisor.status(identity)
    assert ledger.released == []
