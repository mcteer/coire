"""Private durable state refuses competing owners and unsafe filesystem inputs."""

import errno
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import UpgradeMaintenance
from coire_core.upgrade_journal import MAX_DOCUMENT_BYTES, PrivateUpgradeStore


def value() -> UpgradeMaintenance:
    now = datetime.now(UTC)
    return UpgradeMaintenance(
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        generation=1,
        targets=("coire-edge-a",),
        state="draining",
        updated_at=now,
        drain_deadline_at=now,
    )


def test_atomic_restart_and_exclusive_document(tmp_path: Path) -> None:
    store = PrivateUpgradeStore(tmp_path / "private")
    document = value()
    store.write("state.json", document, replace=False)
    assert PrivateUpgradeStore(store.root).read("state.json", UpgradeMaintenance) == document
    with pytest.raises(UpgradeConflict):
        store.write("state.json", document, replace=False)
    assert not list(store.root.glob("*.pending"))
    assert store.path("state.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("boundary", ["file_sync", "replace", "directory_sync"])
def test_full_disk_never_reports_a_durable_success_or_loses_the_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    store = PrivateUpgradeStore(tmp_path / "private")
    original = value()
    store.write("state.json", original)
    changed = UpgradeMaintenance.model_validate(original.model_dump() | {"state": "recovering"})
    sync = os.fsync
    replace = os.replace

    def fail_sync(descriptor: int) -> None:
        directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if directory == (boundary == "directory_sync") and boundary != "replace":
            raise OSError(errno.ENOSPC, "injected full journal filesystem")
        sync(descriptor)

    def fail_replace(source: object, destination: object) -> None:
        if boundary == "replace":
            raise OSError(errno.ENOSPC, "injected full journal filesystem")
        replace(source, destination)

    monkeypatch.setattr(os, "fsync", fail_sync)
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(UpgradeUnavailable, match="persisted"):
        store.write("state.json", changed)
    recovered = PrivateUpgradeStore(store.root).read("state.json", UpgradeMaintenance)
    assert recovered == (changed if boundary == "directory_sync" else original)
    assert recovered is not None and recovered.state in {"draining", "recovering"}
    assert recovered.drain_deadline_at == original.drain_deadline_at
    assert not list(store.root.glob("*.pending"))


def test_cross_descriptor_owner_is_exclusive(tmp_path: Path) -> None:
    store = PrivateUpgradeStore(tmp_path / "private")
    other = PrivateUpgradeStore(store.root)
    with store.ownership(), pytest.raises(UpgradeConflict), other.ownership():
        pass
    with other.ownership():
        pass


@pytest.mark.parametrize("key", ["../outside", "/outside", "", "x" * 129])
def test_untrusted_keys_cannot_escape(tmp_path: Path, key: str) -> None:
    store = PrivateUpgradeStore(tmp_path / "private")
    with pytest.raises(UpgradeConflict):
        store.path(key)


def test_unsafe_permissions_links_and_size_fail_closed(tmp_path: Path) -> None:
    store = PrivateUpgradeStore(tmp_path / "private")
    store.write("state.json", value())
    path = store.path("state.json")
    path.chmod(0o644)
    with pytest.raises(UpgradeUnavailable):
        store.read("state.json", UpgradeMaintenance)
    path.chmod(0o600)
    os.link(path, tmp_path / "alias")
    with pytest.raises(UpgradeUnavailable):
        store.read("state.json", UpgradeMaintenance)
    (tmp_path / "alias").unlink()
    path.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(UpgradeUnavailable):
        store.read("state.json", UpgradeMaintenance)
    path.unlink()
    path.symlink_to(tmp_path / "absent")
    with pytest.raises(UpgradeUnavailable):
        store.write("state.json", value())


def test_duplicate_fields_cannot_change_persisted_authority(tmp_path: Path) -> None:
    store = PrivateUpgradeStore(tmp_path / "private")
    original = value()
    store.path("state.json").write_text('{"generation":999,' + original.model_dump_json()[1:])
    store.path("state.json").chmod(0o600)
    with pytest.raises(UpgradeUnavailable, match="corrupt"):
        store.read("state.json", UpgradeMaintenance)


@pytest.mark.parametrize("reader", ["journal", "owner"])
def test_special_owner_file_is_refused_without_waiting_for_a_writer(
    tmp_path: Path, reader: str
) -> None:
    store = PrivateUpgradeStore(tmp_path / "private")
    os.mkfifo(store.path("state.json"), 0o600)
    program = """
import sys
from pathlib import Path
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import UpgradeMaintenance
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_inventory import read_owner_json
try:
    if sys.argv[2] == 'journal':
        PrivateUpgradeStore(Path(sys.argv[1])).read('state.json', UpgradeMaintenance)
    else:
        read_owner_json(Path(sys.argv[1]) / 'state.json')
except UpgradeUnavailable:
    sys.exit(0)
sys.exit(2)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(store.root), reader], timeout=2, capture_output=True
    )
    assert result.returncode == 0


def test_bounded_journal_wait_preserves_exclusive_owner(tmp_path: Path) -> None:
    import threading

    store = PrivateUpgradeStore(tmp_path / "private")
    acquired, release = threading.Event(), threading.Event()

    def writer() -> None:
        with store.ownership("journal.lock"):
            acquired.set()
            assert release.wait(2)

    worker = threading.Thread(target=writer)
    worker.start()
    assert acquired.wait(2)
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        with store.ownership("journal.lock", wait_s=1):
            assert release.is_set()
            with pytest.raises(UpgradeConflict), store.ownership("journal.lock"):
                pass
    finally:
        release.set()
        timer.cancel()
        worker.join(2)
    assert not worker.is_alive()


def test_bounded_journal_wait_cannot_take_active_owner(tmp_path: Path) -> None:
    import time

    store = PrivateUpgradeStore(tmp_path / "private")
    with store.ownership("journal.lock"):
        started = time.monotonic()
        with pytest.raises(UpgradeConflict), store.ownership("journal.lock", wait_s=0.03):
            pass
        assert time.monotonic() - started < 1
        with pytest.raises(ValueError), store.ownership("journal.lock", wait_s=2):
            pass
