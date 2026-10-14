"""Recovery resolves content-addressed native selections without a source checkout."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NativeRuntimeSelection, PreparedNativeRuntime
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_runtime import environment_content_sha256, seal_tree


def test_isolated_worker_receives_runtime_identity_without_control_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coire_core.settings import Settings
    from coire_node.upgrade_runtime import managed_worker_environment

    settings = Settings(
        upgrades_enabled=True,
        node_name="coire-edge-a",
        upgrade_environment_root=str(tmp_path / "envs"),
        node_token="private-control-token",
    )
    environment = managed_worker_environment(settings)
    assert set(environment) == {"UPGRADES_ENABLED", "NODE_NAME", "UPGRADE_ENVIRONMENT_ROOT"}
    assert "private-control-token" not in environment.values()
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    worker = Settings()
    assert worker.upgrades_enabled
    assert worker.node_name == "coire-edge-a"
    assert worker.upgrade_environment_root == str(tmp_path / "envs")


@pytest.fixture(autouse=True)
def writable_teardown(tmp_path: Path):
    yield
    for path in tmp_path.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)


def prepared(tmp_path: Path) -> tuple[Path, Path, PreparedNativeRuntime]:
    envs, runtimes = tmp_path / "envs", tmp_path / "runtimes"
    environment, interpreter = envs / "source", runtimes / "retained"
    (interpreter / "bin").mkdir(parents=True)
    python = interpreter / "bin/python3"
    python.write_bytes(b"retained-interpreter-fixture")
    python.chmod(0o500)
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python3").symlink_to(python)
    (environment / "module.py").write_bytes(b"source-module")
    seal_tree(interpreter)
    seal_tree(environment)
    value = PreparedNativeRuntime(
        environment_id="source",
        interpreter_id="retained",
        environment_sha256=environment_content_sha256(environment, interpreter),
        interpreter_sha256=environment_content_sha256(interpreter, interpreter),
    )
    PrivateUpgradeStore(envs / ".identities").write("source.json", value)
    return envs, runtimes, value


def test_source_verification_requires_receipt_full_graph_and_retained_interpreter(
    tmp_path: Path,
) -> None:
    from coire_node.upgrade_runtime import verify_prepared_runtime

    envs, runtimes, value = prepared(tmp_path)
    assert verify_prepared_runtime(envs, runtimes, value) == envs / "source/bin/python3"
    with pytest.raises(UpgradeUnavailable):
        verify_prepared_runtime(
            envs, runtimes, value.model_copy(update={"environment_sha256": "a" * 64})
        )
    changed = envs / "source/module.py"
    changed.chmod(0o600)
    changed.write_bytes(b"changed-source")
    changed.chmod(0o400)
    with pytest.raises(UpgradeUnavailable):
        verify_prepared_runtime(envs, runtimes, value)


def test_absent_receipt_or_retained_interpreter_never_selects_source(tmp_path: Path) -> None:
    from coire_node.upgrade_runtime import verify_prepared_runtime

    envs, runtimes, value = prepared(tmp_path)
    (envs / ".identities/source.json").unlink()
    with pytest.raises(UpgradeUnavailable):
        verify_prepared_runtime(envs, runtimes, value)
    assert not (envs / "current").exists()


def test_reconcile_repairs_pointer_from_independent_verified_source_receipt(tmp_path: Path) -> None:
    from coire_node.upgrade_runtime import reconcile_native_selection

    envs, runtimes, value = prepared(tmp_path)
    source = NativeRuntimeSelection(
        node="coire-edge-a",
        release_sha256="a" * 64,
        runtime=value,
        generation=0,
        selected_at=datetime.now(UTC),
    )
    store = PrivateUpgradeStore(tmp_path / "recovery")
    store.write("selection.json", source)
    (envs / "current").symlink_to(envs / "failed-candidate")
    assert reconcile_native_selection(envs, runtimes, store.root, "coire-edge-a") == source
    assert (envs / "current").resolve() == envs / "source"
    assert store.read("selection.json", NativeRuntimeSelection) == source
    with pytest.raises(UpgradeUnavailable):
        reconcile_native_selection(envs, runtimes, store.root, "coire-edge-b")


def test_selection_does_not_overwrite_foreign_pointer_or_unowned_regular_file(
    tmp_path: Path,
) -> None:
    from coire_node.upgrade_runtime import reconcile_native_selection

    envs, runtimes, value = prepared(tmp_path)
    store = PrivateUpgradeStore(tmp_path / "recovery")
    store.write(
        "selection.json",
        NativeRuntimeSelection(
            node="coire-edge-a",
            release_sha256="a" * 64,
            runtime=value,
            generation=0,
            selected_at=datetime.now(UTC),
        ),
    )
    current = envs / "current"
    current.symlink_to(tmp_path / "foreign")
    with pytest.raises(UpgradeUnavailable):
        reconcile_native_selection(envs, runtimes, store.root, "coire-edge-a")
    assert current.is_symlink()
    current.unlink()
    current.write_bytes(b"unowned")
    with pytest.raises(UpgradeUnavailable):
        reconcile_native_selection(envs, runtimes, store.root, "coire-edge-a")
    assert current.read_bytes() == b"unowned"


@pytest.mark.parametrize("fault", ["authority_changed", "boundary_refused"])
def test_pointer_effect_rechecks_authority_after_final_boundary(tmp_path: Path, fault: str) -> None:
    from coire_node.upgrade_runtime import reconcile_native_selection

    envs, runtimes, runtime = prepared(tmp_path)
    source = NativeRuntimeSelection(
        node="coire-edge-a",
        release_sha256="a" * 64,
        runtime=runtime,
        generation=0,
        selected_at=datetime.now(UTC),
    )
    store = PrivateUpgradeStore(tmp_path / "recovery")
    store.write("selection.json", source)
    (envs / "current").symlink_to(envs / "failed-candidate")

    def boundary() -> None:
        if fault == "boundary_refused":
            raise UpgradeUnavailable("peer cannot be observed")
        store.write("selection.json", source.model_copy(update={"release_sha256": "b" * 64}))

    with pytest.raises(UpgradeUnavailable):
        reconcile_native_selection(
            envs, runtimes, store.root, "coire-edge-a", before_replace=boundary
        )
    assert (envs / "current").readlink() == envs / "failed-candidate"
    assert list(envs.glob(".current-*")) == []


def test_cached_graph_detects_same_size_edit_with_restored_mtime(tmp_path: Path) -> None:
    import os

    from coire_node import upgrade_runtime

    environment = tmp_path / "environment"
    interpreter = tmp_path / "interpreter"
    environment.mkdir()
    interpreter.mkdir()
    code = environment / "same-version.py"
    code.write_bytes(b"first-bytes")
    (interpreter / "python").write_bytes(b"owned interpreter")
    first = upgrade_runtime.environment_content_sha256(environment, interpreter)
    assert upgrade_runtime.environment_content_sha256(environment, interpreter) == first
    recorded = code.stat()
    code.write_bytes(b"other-bytes")
    os.utime(code, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))
    assert code.stat().st_size == recorded.st_size
    assert code.stat().st_mtime_ns == recorded.st_mtime_ns
    assert upgrade_runtime.environment_content_sha256(environment, interpreter) != first


def test_cached_graph_still_rejects_new_external_link(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    interpreter = tmp_path / "interpreter"
    environment.mkdir()
    interpreter.mkdir()
    (environment / "module").write_bytes(b"owned")
    (interpreter / "python").write_bytes(b"owned interpreter")
    first = environment_content_sha256(environment, interpreter)
    assert environment_content_sha256(environment, interpreter) == first
    outside = tmp_path / "outside"
    outside.write_bytes(b"untrusted")
    (environment / "external").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        environment_content_sha256(environment, interpreter)


def test_warm_graph_verifies_each_path_without_reopening_unchanged_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_runtime

    environment, interpreter = tmp_path / "env", tmp_path / "runtime"
    environment.mkdir()
    interpreter.mkdir()
    code = environment / "module.py"
    code.write_bytes(b"verified")
    (interpreter / "python").write_bytes(b"interpreter")
    expected = environment_content_sha256(environment, interpreter)
    opened = []
    real_open = upgrade_runtime.os.open

    def open_file(path, *args, **kwargs):
        opened.append(Path(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(upgrade_runtime.os, "open", open_file)
    assert environment_content_sha256(environment, interpreter) == expected
    assert opened == []
    code.write_bytes(b"modified")
    assert environment_content_sha256(environment, interpreter) != expected
    assert opened == [code]


def test_warm_cache_rechecks_complete_metadata_after_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_runtime

    environment, interpreter = tmp_path / "env", tmp_path / "runtime"
    environment.mkdir()
    interpreter.mkdir()
    code = environment / "module.py"
    code.write_bytes(b"verified")
    (interpreter / "python").write_bytes(b"interpreter")
    environment_content_sha256(environment, interpreter)
    real_lstat = Path.lstat
    calls = 0

    def lstat(path, *args, **kwargs):
        nonlocal calls
        if path == code:
            calls += 1
            if calls == 2:
                code.write_bytes(b"modified")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="changed during cached"):
        upgrade_runtime.environment_content_sha256(environment, interpreter)


@pytest.mark.parametrize("change", ["mode", "hardlink"])
def test_warm_cache_cannot_hide_changed_mode_or_link_count(tmp_path: Path, change: str) -> None:
    import os

    environment, interpreter = tmp_path / "env", tmp_path / "runtime"
    environment.mkdir()
    interpreter.mkdir()
    code = environment / "module.py"
    code.write_bytes(b"verified")
    (interpreter / "python").write_bytes(b"interpreter")
    expected = environment_content_sha256(environment, interpreter)
    if change == "mode":
        code.chmod(0o500)
        assert environment_content_sha256(environment, interpreter) != expected
    else:
        os.link(code, tmp_path / "foreign-link")
        with pytest.raises(ValueError, match="link count"):
            environment_content_sha256(environment, interpreter)


@pytest.mark.parametrize("capacity", [4, 8])
def test_cache_working_set_across_three_runtimes_stays_bounded_and_preserves_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capacity: int
) -> None:
    from collections import OrderedDict

    from coire_node import upgrade_runtime

    monkeypatch.setattr(upgrade_runtime, "_content_cache", OrderedDict())
    monkeypatch.setattr(upgrade_runtime, "_CONTENT_CACHE_LIMIT", capacity)
    interpreter = tmp_path / "interpreter"
    interpreter.mkdir()
    (interpreter / "python").write_bytes(b"verified interpreter")
    runtimes = []
    for name in ("source", "support", "predecessor"):
        root = tmp_path / name
        root.mkdir()
        for part in ("one", "two"):
            (root / part).write_bytes((name + part).encode())
        runtimes.append(root)
    expected = [environment_content_sha256(root, interpreter) for root in runtimes]
    opened = []
    real_open = upgrade_runtime.os.open

    def open_file(path, *args, **kwargs):
        opened.append(Path(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(upgrade_runtime.os, "open", open_file)
    assert [environment_content_sha256(root, interpreter) for root in runtimes] == expected
    assert len(upgrade_runtime._content_cache) <= capacity
    if capacity == 8:
        assert opened == []
    else:
        assert opened  # Insufficient capacity causes honest re-reading, never a false digest.
