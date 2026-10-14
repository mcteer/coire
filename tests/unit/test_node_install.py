"""A failed node smoke keeps the previous versioned runtime active."""

from __future__ import annotations

import importlib.util
import io
import os
import platform
import shutil
import struct
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def restore_test_directory_permissions(tmp_path: Path) -> Iterator[None]:
    yield
    # Production retention stays sealed; only this test's owned scratch tree is
    # made writable for pytest's teardown, including dangling interpreter links.
    for directory in tmp_path.rglob("*"):
        if not directory.is_symlink() and directory.is_dir():
            directory.chmod(0o700)


def _installer() -> ModuleType:
    path = Path("apps/coire-node/install_runtime.py")
    spec = importlib.util.spec_from_file_location("coire_node_install_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel_stage() -> ModuleType:
    path = Path("scripts/stage-node-wheels.py")
    spec = importlib.util.spec_from_file_location("coire_node_wheel_stage", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _environment(path: Path) -> Path:
    python = path / "bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text("staged")
    return path


def test_recovery_runtime_verifier_is_packaged_with_node_not_external_checkout() -> None:
    from coire_node.upgrade_runtime import environment_content_sha256

    installer = _installer()
    assert installer.environment_content_sha256 is environment_content_sha256
    assert environment_content_sha256.__module__ == "coire_node.upgrade_runtime"


@pytest.mark.parametrize("bootstrap", ["directory", "dangling_link"])
def test_installer_refuses_out_of_band_managed_activation(tmp_path: Path, bootstrap: str) -> None:
    root = tmp_path / "upgrade-bootstrap"
    if bootstrap == "directory":
        root.mkdir()
    else:
        root.symlink_to(tmp_path / "missing-recovery-state")
    result = subprocess.run(
        ["bash", "apps/coire-node/install.sh", "--prefix", str(tmp_path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert b"activate through the admin upgrade API" in result.stderr
    assert not (tmp_path / "bin").exists()
    assert not (tmp_path / "envs/current").exists()


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_runtime_input_digest_rejects_linked_inputs(tmp_path: Path, link: str) -> None:
    installer = _installer()
    source = tmp_path / "source"
    source.write_bytes(b"trusted-input")
    artifact = tmp_path / "wheel"
    if link == "symbolic":
        artifact.symlink_to(source)
    else:
        os.link(source, artifact)
    with pytest.raises((ValueError, OSError)):
        installer.artifact_graph_sha256([artifact])


def test_failed_smoke_keeps_previous_link_and_staged_environment(tmp_path: Path) -> None:
    installer = _installer()
    previous = _environment(tmp_path / "envs/old")
    staged = _environment(tmp_path / "envs/new.staging")
    target = tmp_path / "envs/new"
    current = tmp_path / "envs/current"
    current.symlink_to(previous)

    def fail(_python: Path) -> None:
        raise subprocess.CalledProcessError(1, "vision smoke")

    with pytest.raises(subprocess.CalledProcessError):
        installer.publish_environment(staged, target, current, verify=fail)
    assert current.resolve() == previous
    assert staged.is_dir() and not target.exists()


def test_stage_only_verifies_and_installs_without_activating(tmp_path: Path) -> None:
    installer = _installer()
    previous = _environment(tmp_path / "envs/old")
    staged = _environment(tmp_path / "envs/new.staging")
    target = tmp_path / "envs/new"
    current = tmp_path / "envs/current"
    current.symlink_to(previous)
    checked: list[Path] = []
    installer.stage_environment(staged, target, verify=checked.append)
    assert checked == [staged / "bin/python3"]
    assert target.is_dir() and not staged.exists()
    assert current.resolve() == previous
    installer.stage_environment(target, target, verify=checked.append)
    assert checked[-1] == target / "bin/python3"
    assert current.resolve() == previous


def test_stage_only_refuses_existing_target_without_overwrite(tmp_path: Path) -> None:
    installer = _installer()
    staged = _environment(tmp_path / "envs/new.staging")
    target = _environment(tmp_path / "envs/existing")
    with pytest.raises(FileExistsError):
        installer.stage_environment(staged, target, verify=lambda _: None)
    assert staged.is_dir() and target.is_dir()


@pytest.mark.parametrize("already_installed", [False, True])
def test_staging_rejects_wrong_interpreter_before_smoke_or_activation(
    tmp_path: Path, already_installed: bool
) -> None:
    installer = _installer()
    target = tmp_path / "envs/new"
    source = _environment(target if already_installed else tmp_path / "envs/new.staging")
    expected = tmp_path / "runtime/bin/coire-node-python"
    expected.parent.mkdir(parents=True)
    expected.write_text("dedicated interpreter")
    checked: list[Path] = []
    with pytest.raises(ValueError, match="interpreter identity"):
        installer.stage_environment(source, target, verify=checked.append, expected_python=expected)
    assert checked == [] and source.is_dir()
    if not already_installed:
        assert not target.exists()


def test_correct_interpreter_binding_survives_staging_and_publication(tmp_path: Path) -> None:
    installer = _installer()
    expected = tmp_path / "runtime/bin/coire-node-python"
    expected.parent.mkdir(parents=True)
    expected.write_text("dedicated interpreter")
    source = tmp_path / "envs/new.staging"
    (source / "bin").mkdir(parents=True)
    (source / "bin/python").symlink_to(expected)
    (source / "bin/python3").symlink_to("python")
    target = tmp_path / "envs/new"
    current = tmp_path / "envs/current"
    previous = _environment(tmp_path / "envs/old")
    current.symlink_to(previous)
    installer.publish_environment(
        source, target, current, verify=lambda _: None, expected_python=expected
    )
    assert current.resolve() == target
    assert (current / "bin/python3").resolve() == expected


def test_network_python_preserves_shared_interpreter_and_reuses_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _installer()
    source = tmp_path / "python3.13"
    original = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
    original += struct.pack("<2I", 0x1B, 24) + bytes(range(16)) + b"executable code"
    source.write_bytes(original)
    source.chmod(0o755)
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, "", "Identifier=com.coire.node.runtime\n")
    )
    monkeypatch.setattr(installer.subprocess, "run", run)
    target = installer.network_python(source)
    assert target.name == "coire-node-python"
    assert source.read_bytes() == original
    copied = target.read_bytes()
    assert copied[:40] == original[:40] and copied[56:] == original[56:]
    assert copied[40:56] != original[40:56]
    assert target.stat().st_mode & 0o111
    run.reset_mock()
    assert installer.network_python(source) == target
    assert all("--force" not in call.args[0] for call in run.call_args_list)


def test_network_python_sign_failure_leaves_source_and_no_partial_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _installer()
    source = tmp_path / "python3.13"
    original = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
    original += struct.pack("<2I", 0x1B, 24) + bytes(range(16))
    source.write_bytes(original)
    monkeypatch.setattr(
        installer.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, "codesign"))
    )
    with pytest.raises(subprocess.CalledProcessError):
        installer.network_python(source)
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "contents", [b"short", bytes(32), struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)]
)
def test_network_python_rejects_malformed_binary_without_publishing(
    tmp_path: Path, contents: bytes
) -> None:
    source = tmp_path / "python3.13"
    source.write_bytes(contents)
    with pytest.raises(ValueError):
        _installer().network_python(source)
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == contents


def test_network_python_rejects_existing_wrong_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _installer()
    source = tmp_path / "python3.13"
    source.write_bytes(b"shared")
    target = tmp_path / "coire-node-python"
    target.write_bytes(b"existing")
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, "", "Identifier=other\n")),
    )
    with pytest.raises(ValueError, match="signing identity"):
        installer.network_python(source)
    assert target.read_bytes() == b"existing"


def test_network_python_rejects_signed_copy_with_shared_build_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _installer()
    source = tmp_path / "python3.13"
    original = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
    original += struct.pack("<2I", 0x1B, 24) + bytes(range(16))
    source.write_bytes(original)
    target = tmp_path / "coire-node-python"
    target.write_bytes(original)
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, "", "Identifier=com.coire.node.runtime\n"
            )
        ),
    )
    with pytest.raises(ValueError, match="build UUID"):
        installer.network_python(source)
    assert source.read_bytes() == target.read_bytes() == original


def test_successful_smoke_moves_immutable_environment_before_link_flip(tmp_path: Path) -> None:
    installer = _installer()
    previous = _environment(tmp_path / "envs/old")
    staged = _environment(tmp_path / "envs/new.staging")
    target = tmp_path / "envs/new"
    current = tmp_path / "envs/current"
    current.symlink_to(previous)
    seen: list[Path] = []

    def verify(python: Path) -> None:
        seen.append(python)
        assert staged.is_dir() and current.resolve() == previous

    installer.publish_environment(staged, target, current, verify=verify)
    assert seen == [staged / "bin/python3"]
    assert target.is_dir() and not staged.exists()
    assert current.resolve() == target


def test_smoke_checks_text_and_vision_cli_without_starting_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer()
    run = Mock()
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform, "node", lambda: "coire-edge-a")
    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setenv("HF_TOKEN", "must-not-reach-smoke")
    monkeypatch.setenv("WANDB_API_KEY", "must-not-reach-smoke")
    monkeypatch.setenv("DATABASE_URL", "must-not-reach-smoke")
    installer.smoke(Path("/staged/bin/python3"))
    expected = [
        [
            "/staged/bin/python3",
            "-c",
            (
                "import coire_core, coire_node, mlx_lm, mlx_vlm, mflux; "
                "import coire_node.upgrade_guard, coire_node.expired_recovery_support, "
                "coire_node.expired_recovery_successor_session"
            ),
        ],
        ["/staged/bin/python3", "-m", "mlx_lm.server", "--help"],
        ["/staged/bin/python3", "-m", "mlx_vlm.server", "--help"],
        ["/staged/bin/python3", "-c", "from mflux.models.z_image.variants.z_image import ZImage"],
    ]
    assert [call.args[0] for call in run.call_args_list[:4]] == [
        [command[0], "-I", "-B", *command[1:]] for command in expected
    ]
    assert len(run.call_args_list) == 5
    assert all("DATABASE_URL" not in call.kwargs["env"] for call in run.call_args_list)
    training_probe = run.call_args_list[4].args[0]
    assert training_probe[:4] == ["/staged/bin/python3", "-I", "-B", "-c"]
    assert "inspect.signature(train)" in training_probe[4]
    assert "training_callback" in training_probe[4]
    assert "version('mlx') == '0.32.2'" in training_probe[4]
    assert "version('mlx-lm') == '0.31.3'" in training_probe[4]
    assert "coire_node.training.worker" in training_probe[4]
    for call in run.call_args_list:
        env = call.kwargs["env"]
        assert env["HF_HUB_OFFLINE"] == "1"
        assert env["TRANSFORMERS_OFFLINE"] == "1"
        assert env["WANDB_MODE"] == "disabled"
        assert "HF_TOKEN" not in env and "WANDB_API_KEY" not in env


def test_smoke_refuses_core_before_starting_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    installer = _installer()
    run = Mock()
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform, "node", lambda: "coire-core")
    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(ValueError, match="registered Apple Silicon Studio"):
        installer.smoke(Path("/staged/bin/python3"))
    run.assert_not_called()


def test_image_runtime_is_darwin_only_and_locked() -> None:
    node = tomllib.loads(Path("apps/coire-node/pyproject.toml").read_text())
    assert "mflux==0.20.0; platform_system=='Darwin'" in node["project"]["dependencies"]
    lock = tomllib.loads(Path("uv.lock").read_text())
    packages = {package["name"]: package for package in lock["package"]}
    assert packages["mflux"]["version"] == "0.20.0"
    assert any(
        wheel["url"].startswith("https://files.pythonhosted.org/")
        and wheel["hash"].startswith("sha256:")
        for wheel in packages["mflux"]["wheels"]
    )
    assert any(
        dependency["name"] == "mflux" and dependency.get("marker") == "sys_platform == 'darwin'"
        for dependency in packages["coire-node"]["dependencies"]
    )
    for name in ("coire-api", "coire-agent", "coire-core", "coire-file-worker"):
        assert all(
            dependency["name"] != "mflux" for dependency in packages[name].get("dependencies", [])
        )


def test_frozen_node_wheel_selection_includes_image_runtime_only_on_darwin(
    tmp_path: Path,
) -> None:
    pylock = tmp_path / "pylock.node.toml"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--package",
            "coire-node",
            "--no-dev",
            "--no-emit-workspace",
            "--format",
            "pylock.toml",
            "--output-file",
            str(pylock),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    selected = _wheel_stage().locked_wheels(pylock)
    mflux = [url for url, _digest, _size in selected if "/mflux-0.20.0-" in url]
    assert bool(mflux) is (platform.system() == "Darwin")


def test_wheel_hash_failure_never_publishes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _wheel_stage()
    monkeypatch.setattr(
        stage.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"wrong")
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        stage.stage(
            [("https://files.pythonhosted.org/packages/demo-1.0-py3-none-any.whl", "0" * 64, 5)],
            tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


def test_runtime_content_identity_uses_complete_named_inputs(tmp_path: Path) -> None:
    installer = _installer()
    first, second = tmp_path / "first.whl", tmp_path / "second.whl"
    first.write_bytes(b"ab")
    second.write_bytes(b"c")
    original = installer.artifact_graph_sha256([first, second])
    assert len(original) == 64
    assert installer.artifact_graph_sha256([second, first]) == original
    first.write_bytes(b"a")
    second.write_bytes(b"bc")
    assert installer.artifact_graph_sha256([first, second]) != original
    with pytest.raises(ValueError, match="distinct names"):
        installer.artifact_graph_sha256([first, first])


def test_environment_identity_covers_native_and_interpreter_bytes(tmp_path: Path) -> None:
    installer = _installer()
    env = _environment(tmp_path / "env")
    base = tmp_path / "python"
    base.mkdir()
    library = base / "libpython.dylib"
    library.write_bytes(b"native")
    (env / "bin/python3").unlink()
    binary = base / "python3"
    binary.write_bytes(b"interpreter")
    (env / "bin/python3").symlink_to(binary)
    first = installer.environment_content_sha256(env, base)
    library.write_bytes(b"changed")
    assert installer.environment_content_sha256(env, base) != first
    library.write_bytes(b"native")
    assert installer.environment_content_sha256(env, base) == first
    outside = tmp_path / "outside"
    outside.write_bytes(b"uncontrolled")
    (env / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        installer.environment_content_sha256(env, base)


def test_retained_interpreter_survives_shared_interpreter_removal(tmp_path: Path) -> None:
    installer = _installer()
    shared = tmp_path / "shared"
    (shared / "bin").mkdir(parents=True)
    (shared / "lib").mkdir()
    (shared / "bin/python3.13").write_bytes(b"binary")
    (shared / "lib/libpython.dylib").write_bytes(b"library")
    (shared / "bin/python3").symlink_to(shared / "bin/python3.13")
    target = tmp_path / "retained"
    installer.retain_interpreter(shared, target)
    assert (target / "bin/python3").resolve() == target / "bin/python3.13"
    installer.shutil.rmtree(shared)
    assert (target / "bin/python3").read_bytes() == b"binary"
    assert (target / "lib/libpython.dylib").read_bytes() == b"library"
    with pytest.raises(FileExistsError):
        installer.retain_interpreter(target, target)


def test_retention_refuses_external_interpreter_dependencies_without_publication(
    tmp_path: Path,
) -> None:
    installer = _installer()
    shared = tmp_path / "shared"
    shared.mkdir()
    outside = tmp_path / "external-library"
    outside.write_bytes(b"mutable library")
    (shared / "library").symlink_to(outside)
    target = tmp_path / "retained"
    with pytest.raises(ValueError, match="outside"):
        installer.retain_interpreter(shared, target)
    assert not target.exists()
    assert set(tmp_path.iterdir()) == {shared, outside}


def test_prepared_interpreter_is_sealed_reused_and_content_verified(tmp_path: Path) -> None:
    installer = _installer()
    shared = tmp_path / "shared"
    (shared / "bin").mkdir(parents=True)
    binary = shared / "bin/python3"
    binary.write_bytes(b"interpreter")
    binary.chmod(0o755)
    library = shared / "libpython.dylib"
    library.write_bytes(b"library")
    store = tmp_path / "retained"
    prepared = installer.prepare_interpreter(
        shared, binary, store, prepare_binary=lambda path: path
    )
    assert prepared.read_bytes() == b"interpreter"
    retained = prepared.parent.parent
    assert retained != shared
    assert all(
        not (path.stat().st_mode & 0o222) for path in retained.rglob("*") if not path.is_symlink()
    )
    assert (
        installer.prepare_interpreter(shared, binary, store, prepare_binary=lambda path: path)
        == prepared
    )
    retained_library = retained / "libpython.dylib"
    retained_library.chmod(0o600)
    retained_library.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="content"):
        installer.prepare_interpreter(shared, binary, store, prepare_binary=lambda path: path)
    assert library.read_bytes() == b"library"


def test_sealed_environment_receipt_verifies_every_byte_before_reuse(tmp_path: Path) -> None:
    installer = _installer()
    interpreter = tmp_path / "runtimes" / ("a" * 64)
    (interpreter / "bin").mkdir(parents=True)
    python = interpreter / "bin/python3"
    python.write_bytes(b"interpreter")
    python.chmod(0o755)
    installer.seal_tree(interpreter)
    source = tmp_path / "envs/staged"
    (source / "bin").mkdir(parents=True)
    (source / "bin/python3").symlink_to(python)
    module = source / "native.so"
    module.write_bytes(b"native")
    target = tmp_path / "envs/candidate"
    checked: list[Path] = []
    receipt = installer.stage_sealed_environment(source, target, python, verify=checked.append)
    assert len(receipt.environment_sha256) == 64
    assert checked == [source / "bin/python3"]
    assert not (target / "native.so").stat().st_mode & 0o222
    assert (
        installer.stage_sealed_environment(target, target, python, verify=checked.append) == receipt
    )
    changed = target / "native.so"
    changed.chmod(0o600)
    changed.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="content"):
        installer.stage_sealed_environment(target, target, python, verify=checked.append)
    assert len(checked) == 2


def test_interpreter_receipt_rejects_duplicate_fields(tmp_path: Path) -> None:
    installer = _installer()
    shared = tmp_path / "shared"
    (shared / "bin").mkdir(parents=True)
    binary = shared / "bin/python3"
    binary.write_bytes(b"interpreter")
    binary.chmod(0o755)
    store = tmp_path / "retained"
    installer.prepare_interpreter(shared, binary, store, prepare_binary=lambda path: path)
    receipt = next(store.glob("*.json"))
    content = receipt.read_text()
    receipt.write_text('{"executable":"outside",' + content[1:])
    with pytest.raises(ValueError, match="duplicate"):
        installer.prepare_interpreter(shared, binary, store, prepare_binary=lambda path: path)


def test_shipped_installer_bootstraps_without_an_installed_node_package(tmp_path: Path) -> None:
    bundle = tmp_path / "apps/coire-node"
    source = bundle / "src/coire_node"
    source.mkdir(parents=True)
    shutil.copyfile("apps/coire-node/install_runtime.py", bundle / "install_runtime.py")
    for name in ("__init__.py", "upgrade_runtime.py"):
        shutil.copyfile(Path("apps/coire-node/src/coire_node") / name, source / name)
    first, second = tmp_path / "first.whl", tmp_path / "second.whl"
    first.write_bytes(b"first immutable input")
    second.write_bytes(b"second immutable input")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(bundle / "install_runtime.py"),
            "--content-id",
            str(first),
            str(second),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == _installer().artifact_graph_sha256([first, second])
    shipping = Path("scripts/build-node-wheel.sh").read_text()
    assert "coire-stage/apps/coire-node/src/coire_node" in shipping
    assert "apps/coire-node/src/coire_node/upgrade_runtime.py" in shipping
    assert "deploy/launchd/com.coire.upgrade-guard.plist.template" in shipping
