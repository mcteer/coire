"""A failed node smoke keeps the previous versioned runtime active."""

from __future__ import annotations

import importlib.util
import io
import platform
import struct
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


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
    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setenv("HF_TOKEN", "must-not-reach-smoke")
    monkeypatch.setenv("WANDB_API_KEY", "must-not-reach-smoke")
    installer.smoke(Path("/staged/bin/python3"))
    assert [call.args[0] for call in run.call_args_list[:4]] == [
        ["/staged/bin/python3", "-c", "import coire_core, coire_node, mlx_lm, mlx_vlm, mflux"],
        ["/staged/bin/python3", "-m", "mlx_lm.server", "--help"],
        ["/staged/bin/python3", "-m", "mlx_vlm.server", "--help"],
        ["/staged/bin/python3", "-c", "from mflux.models.z_image.variants.z_image import ZImage"],
    ]
    assert len(run.call_args_list) == 5
    training_probe = run.call_args_list[4].args[0]
    assert training_probe[:2] == ["/staged/bin/python3", "-c"]
    assert "inspect.signature(train)" in training_probe[2]
    assert "training_callback" in training_probe[2]
    assert "version('mlx') == '0.32.2'" in training_probe[2]
    assert "version('mlx-lm') == '0.31.3'" in training_probe[2]
    assert "coire_node.training.worker" in training_probe[2]
    for call in run.call_args_list:
        env = call.kwargs["env"]
        assert env["HF_HUB_OFFLINE"] == "1"
        assert env["TRANSFORMERS_OFFLINE"] == "1"
        assert env["WANDB_MODE"] == "disabled"
        assert "HF_TOKEN" not in env and "WANDB_API_KEY" not in env


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
