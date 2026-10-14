"""Owned staging space survives faults and never consumes or removes user assets."""

from pathlib import Path

import pytest
from upgrade_fixtures import plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable


def test_physical_disk_hold_replay_and_positive_cleanup(tmp_path: Path) -> None:
    from coire_node.upgrade_executor import NativeDiskReservation

    ledger = NativeDiskReservation(tmp_path / "space", plan())
    hold = ledger.reserve(32 * 1024)
    assert hold.state == "held" and hold.allocated_bytes >= hold.requested_bytes
    path = ledger.store.path(hold.resource_key)
    assert path.stat().st_blocks * 512 >= hold.requested_bytes
    assert ledger.reserve(hold.requested_bytes) == hold
    with pytest.raises(UpgradeConflict):
        ledger.reserve(hold.requested_bytes + 1)
    released = ledger.release()
    assert released is not None and released.state == "released" and not path.exists()
    assert ledger.release() == released


def test_allocation_failure_keeps_owned_hold_until_explicit_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_node import upgrade_executor

    ledger = upgrade_executor.NativeDiskReservation(tmp_path / "space", plan())
    monkeypatch.setattr(
        upgrade_executor, "preallocate_disk", lambda *_: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(UpgradeUnavailable):
        ledger.reserve(32 * 1024)
    hold = ledger.current()
    assert hold is not None and hold.state == "allocating" and hold.file_inode is not None
    assert ledger.store.path(hold.resource_key).exists()
    released = ledger.release()
    assert released is not None and released.state == "released"


def test_replaced_reservation_file_is_never_unlinked(tmp_path: Path) -> None:
    from coire_node.upgrade_executor import NativeDiskReservation

    ledger = NativeDiskReservation(tmp_path / "space", plan())
    hold = ledger.reserve(32 * 1024)
    path = ledger.store.path(hold.resource_key)
    path.rename(ledger.store.path("original-reservation"))
    path.write_bytes(b"foreign-file")
    path.chmod(0o600)
    with pytest.raises(UpgradeUnavailable):
        ledger.release()
    assert path.read_bytes() == b"foreign-file"
    assert ledger.current().state == "held"


def test_unexplained_missing_inode_retains_disk_ownership(tmp_path: Path) -> None:
    from coire_node.upgrade_executor import NativeDiskReservation

    ledger = NativeDiskReservation(tmp_path / "space", plan())
    hold = ledger.reserve(32 * 1024)
    ledger.store.path(hold.resource_key).rename(tmp_path / "moved-reservation")
    with pytest.raises(UpgradeUnavailable):
        ledger.release()
    assert ledger.current().state == "held"


def test_restart_reconciles_unlink_only_after_durable_cleanup_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_node.upgrade_executor import NativeDiskReservation

    ledger = NativeDiskReservation(tmp_path / "space", plan())
    hold = ledger.reserve(32 * 1024)
    unlink = Path.unlink
    path = ledger.store.path(hold.resource_key)

    def interrupted(target: Path, *args: object, **kwargs: object) -> None:
        if target == path:
            unlink(target)
            raise UpgradeUnavailable("simulated crash after owned unlink")
        unlink(target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(UpgradeUnavailable):
            ledger.release()
    assert ledger.current().state == "releasing"
    assert not path.exists()
    restarted = NativeDiskReservation(ledger.store.root, ledger.plan)
    assert restarted.release().state == "released"


@pytest.mark.parametrize(
    "fault", [None, "inode", "not_rebooted", "duplicate", "move_lost", "hold_lost"]
)
def test_terminal_quarantine_preserves_reboot_renumbered_bytes(tmp_path, monkeypatch, fault):
    from datetime import UTC, datetime, timedelta

    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node import upgrade_native_terminal as ack
    from coire_node.upgrade_executor import NativeDiskReservation

    ledger = NativeDiskReservation(tmp_path / "space", plan())
    hold = ledger.reserve(32 * 1024)
    source = ledger.store.path(hold.resource_key)
    original = source.read_bytes()
    recorded = hold.model_copy(
        update={
            "file_device": hold.file_device + 1,
            "updated_at": datetime.now(UTC) - timedelta(days=2),
        }
    )
    ledger._record(recorded)
    run = PrivateUpgradeStore(tmp_path / "run")
    destination = run.path("native-terminal.staging-reservation.quarantine")
    monkeypatch.setattr(
        ack.psutil, "boot_time", lambda: (datetime.now(UTC) - timedelta(days=1)).timestamp()
    )
    if fault == "inode":
        ledger._record(recorded.model_copy(update={"file_inode": recorded.file_inode + 1}))
    elif fault == "not_rebooted":
        monkeypatch.setattr(ack.psutil, "boot_time", lambda: recorded.updated_at.timestamp() - 1)
    elif fault == "duplicate":
        destination.write_bytes(b"foreign")
    if fault in {"inode", "not_rebooted", "duplicate"}:
        with pytest.raises(UpgradeUnavailable):
            ack.quarantine_rebooted_staging(ledger, run, "a" * 64)
        assert source.read_bytes() == original
        assert ledger.current().state == "held"
        return
    if fault in {"move_lost", "hold_lost"}:
        with monkeypatch.context() as patch:
            if fault == "move_lost":
                rename = Path.rename

                def lost_move(self, target):
                    rename(self, target)
                    raise UpgradeUnavailable("lost move")

                patch.setattr(Path, "rename", lost_move)
            else:
                record = NativeDiskReservation._record

                def lost_hold(self, value):
                    record(self, value)
                    raise UpgradeUnavailable("lost hold")

                patch.setattr(NativeDiskReservation, "_record", lost_hold)
            with pytest.raises(UpgradeUnavailable, match="lost"):
                ack.quarantine_rebooted_staging(ledger, run, "a" * 64)
    assert ack.quarantine_rebooted_staging(ledger, run, "a" * 64)
    assert ack.quarantine_rebooted_staging(ledger, run, "a" * 64)
    assert destination.read_bytes() == original and not source.exists()
    retired = ledger.current()
    assert retired.state == "released" and retired.file_device == recorded.file_device
    assert ledger.release() == retired
    with pytest.raises(UpgradeConflict):
        ack.quarantine_rebooted_staging(ledger, run, "b" * 64)
