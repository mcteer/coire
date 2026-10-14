"""A copied owner DTO cannot authorize publication outside a held lease scope."""

import os
import subprocess
import sys
from contextvars import copy_context

import pytest

from coire_core.errors import UpgradeUnavailable
from coire_core.upgrade_journal import PrivateUpgradeStore


def test_actual_fixture_descriptor_survives_parent_close_in_fixed_child(tmp_path):
    from coire_core.errors import UpgradeConflict
    from coire_node.recovery_lease import bind_recovery_lease, recovery_supervision_descriptor

    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    records = PrivateUpgradeStore(tmp_path / "fixture-records")
    path = records.path("fixture-supervisor.lock")
    child = None
    try:
        with (
            bootstrap.ownership("recovery-transfer.lock") as recovery,
            records.ownership(path.name) as supervision,
            bind_recovery_lease(bootstrap, "a" * 64, recovery, supervision_descriptor=supervision),
        ):
            inherited = recovery_supervision_descriptor(bootstrap, "a" * 64, path)
            assert inherited == supervision
            script = (
                "import os,sys; os.fstat(int(sys.argv[1])); "
                "print('inherited',flush=True); sys.stdin.buffer.read()"
            )
            child = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", script, str(inherited)],
                pass_fds=(inherited,),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert child.stdout.readline() == b"inherited\n"
            with pytest.raises(UpgradeConflict), records.ownership(path.name):
                pass
        # The parent's ownership context and non-wire capability have ended.
        # A different open description still cannot acquire the fixture lock.
        with pytest.raises(UpgradeConflict), records.ownership(path.name):
            pass
        child.communicate(timeout=5)
        assert child.returncode == 0
        with records.ownership(path.name):
            pass
    finally:
        if child is not None and child.poll() is None:
            child.communicate(timeout=5)


@pytest.mark.parametrize("fault", ["no_scope", "missing", "foreign", "unlocked"])
def test_fixture_descriptor_requires_actual_bound_private_exclusion(tmp_path, fault):
    from coire_node.recovery_lease import bind_recovery_lease, recovery_supervision_descriptor

    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    records = PrivateUpgradeStore(tmp_path / "fixture-records")
    path = records.path("fixture-supervisor.lock")
    if fault == "no_scope":
        with pytest.raises(UpgradeUnavailable):
            recovery_supervision_descriptor(bootstrap, "a" * 64, path)
        return
    with bootstrap.ownership("recovery-transfer.lock") as recovery:
        if fault == "unlocked":
            path.touch(mode=0o600)
            descriptor = os.open(path, os.O_RDWR)
            try:
                with (
                    bind_recovery_lease(
                        bootstrap, "a" * 64, recovery, supervision_descriptor=descriptor
                    ),
                    pytest.raises(UpgradeUnavailable, match="not locked"),
                ):
                    recovery_supervision_descriptor(bootstrap, "a" * 64, path)
            finally:
                os.close(descriptor)
        else:
            with (
                records.ownership(path.name) as descriptor,
                bind_recovery_lease(
                    bootstrap,
                    "a" * 64,
                    recovery,
                    supervision_descriptor=descriptor if fault != "missing" else None,
                ),
                pytest.raises(UpgradeUnavailable),
            ):
                recovery_supervision_descriptor(
                    bootstrap,
                    "a" * 64,
                    records.path("foreign.lock") if fault == "foreign" else path,
                )


def test_cold_source_handoff_releases_writer_and_retains_recovery_exclusion(tmp_path):
    from coire_core.errors import UpgradeConflict
    from coire_node.recovery_lease import (
        bind_recovery_lease,
        handoff_recovery_lifetime,
        require_recovery_lease,
    )

    store = PrivateUpgradeStore(tmp_path / "bootstrap")
    with (
        store.ownership("lifetime.lock") as lifetime,
        store.ownership("recovery-transfer.lock") as recovery,
        bind_recovery_lease(store, "a" * 64, recovery, lifetime_descriptor=lifetime),
    ):
        with pytest.raises(UpgradeConflict), store.ownership("lifetime.lock", wait_s=0):
            pass
        handoff_recovery_lifetime(store, "a" * 64)
        require_recovery_lease(store, "a" * 64)
        with store.ownership("lifetime.lock", wait_s=0):
            handoff_recovery_lifetime(store, "a" * 64)  # Historical replay never unlocks Source.
            with pytest.raises(UpgradeConflict), store.ownership("lifetime.lock", wait_s=0):
                pass
        with pytest.raises(UpgradeConflict), store.ownership("recovery-transfer.lock", wait_s=0):
            pass


def test_portable_claim_or_plain_recovery_lease_cannot_relinquish_writer(tmp_path):
    from coire_node.recovery_lease import bind_recovery_lease, handoff_recovery_lifetime

    store = PrivateUpgradeStore(tmp_path / "bootstrap")
    with (
        store.ownership("recovery-transfer.lock") as recovery,
        bind_recovery_lease(store, "a" * 64, recovery),
        pytest.raises(UpgradeUnavailable, match="actual cold lifetime descriptor"),
    ):
        handoff_recovery_lifetime(store, "a" * 64)


@pytest.mark.parametrize("fault", [None, "wrong_scope", "unlocked", "closed"])
def test_recovery_capability_requires_live_descriptor_and_scope(tmp_path, fault):
    from coire_node.recovery_lease import bind_recovery_lease, require_recovery_lease

    store = PrivateUpgradeStore(tmp_path / "bootstrap")
    sha = "a" * 64
    if fault == "unlocked":
        fd = os.open(store.path("recovery-transfer.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(UpgradeUnavailable), bind_recovery_lease(store, sha, fd):
                pytest.fail("unlocked descriptor authorized")
        finally:
            os.close(fd)
        return
    with store.ownership("recovery-transfer.lock") as fd:
        with bind_recovery_lease(store, sha, fd):
            if fault == "wrong_scope":
                with pytest.raises(UpgradeUnavailable):
                    require_recovery_lease(store, "b" * 64)
            elif fault == "closed":
                copied = os.dup(fd)
                try:
                    with bind_recovery_lease(store, sha, copied):
                        os.close(copied)
                        with pytest.raises(UpgradeUnavailable):
                            require_recovery_lease(store, sha)
                finally:
                    # Closed descriptor is intentionally not closed twice.
                    pass
            else:
                require_recovery_lease(store, sha)
        with pytest.raises(UpgradeUnavailable):
            require_recovery_lease(store, sha)


def test_revoked_capability_during_lock_probe_is_refused(tmp_path, monkeypatch):
    from coire_node import recovery_lease as module

    store = PrivateUpgradeStore(tmp_path / "bootstrap")
    sha = "a" * 64
    with (
        store.ownership("recovery-transfer.lock") as fd,
        module.bind_recovery_lease(store, sha, fd),
    ):
        capability = module._scope.get()
        assert capability is not None
        flock = module.fcntl.flock

        def revoke_during_probe(descriptor, operation):
            try:
                return flock(descriptor, operation)
            finally:
                capability.released.set()

        with monkeypatch.context() as patch:
            patch.setattr(module.fcntl, "flock", revoke_during_probe)
            with pytest.raises(UpgradeUnavailable):
                module.require_recovery_lease(store, sha)


def test_copied_context_cannot_reuse_exited_binding_with_still_held_descriptor(tmp_path):
    from coire_node.recovery_lease import bind_recovery_lease, require_recovery_lease

    store = PrivateUpgradeStore(tmp_path / "bootstrap")
    sha = "a" * 64
    with store.ownership("recovery-transfer.lock") as fd:
        with bind_recovery_lease(store, sha, fd):
            copied = copy_context()
            copied.run(require_recovery_lease, store, sha)
        # The actual descriptor remains valid and locked, but its ended binding
        # must not survive in a child task's inherited context.
        with pytest.raises(UpgradeUnavailable):
            copied.run(require_recovery_lease, store, sha)
        with bind_recovery_lease(store, sha, fd):
            require_recovery_lease(store, sha)
            with pytest.raises(UpgradeUnavailable):
                copied.run(require_recovery_lease, store, sha)
