"""Installed plist reader rejects links, writable/unowned/nonregular preimages."""

import os

import pytest

from coire_core.errors import UpgradeUnavailable


def test_definition_reader_refuses_replaced_parent_path(tmp_path, monkeypatch):
    from coire_node import expired_recovery_definition as module

    parent = tmp_path / "installed"
    parent.mkdir()
    path = parent / "guard.plist"
    path.write_bytes(b"original definition")
    path.chmod(0o600)
    fstat = module.os.fstat
    calls = []

    def replace_parent_during_read(fd):
        calls.append(fd)
        if len(calls) == 2:
            parent.rename(tmp_path / "retained")
            parent.mkdir()
            path.write_bytes(b"replacement definition")
            path.chmod(0o600)
        return fstat(fd)

    monkeypatch.setattr(module.os, "fstat", replace_parent_during_read)
    with pytest.raises(UpgradeUnavailable):
        module.read_definition_file(path, os.getuid())


def test_definition_reader_refuses_inplace_change_before_final_path_check(tmp_path, monkeypatch):
    from coire_node import expired_recovery_definition as module

    path = tmp_path / "guard.plist"
    path.write_bytes(b"original definition")
    path.chmod(0o600)
    lstat = module.Path.lstat

    def changed_path(current, *args, **kwargs):
        if current == path:
            path.write_bytes(b"replacement definition with different content")
        return lstat(current, *args, **kwargs)

    monkeypatch.setattr(module.Path, "lstat", changed_path)
    with pytest.raises(UpgradeUnavailable):
        module.read_definition_file(path, os.getuid())


@pytest.mark.parametrize("fault", [None, "link", "writable", "oversize", "directory"])
def test_definition_file_reads_only_safe_stable_preimages(tmp_path, fault):
    from coire_node.expired_recovery_definition import read_definition_file

    path = tmp_path / "guard.plist"
    path.write_bytes(b"fixture definition")
    path.chmod(0o600)
    if fault == "link":
        linked = tmp_path / "linked"
        linked.symlink_to(path)
        path = linked
    elif fault == "writable":
        path.chmod(0o666)
    elif fault == "oversize":
        path.write_bytes(b"x" * (64 * 1024 + 1))
    elif fault == "directory":
        path.unlink()
        path.mkdir()
    if fault:
        with pytest.raises(UpgradeUnavailable):
            read_definition_file(path, os.getuid())
    else:
        assert read_definition_file(path, os.getuid()) == b"fixture definition"
