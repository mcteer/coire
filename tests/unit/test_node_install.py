"""A failed node smoke keeps the previous versioned runtime active."""

from __future__ import annotations

import importlib.util
import io
import platform
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
    installer.smoke(Path("/staged/bin/python3"))
    assert [call.args[0] for call in run.call_args_list] == [
        ["/staged/bin/python3", "-c", "import coire_core, coire_node, mlx_lm, mlx_vlm, mflux"],
        ["/staged/bin/python3", "-m", "mlx_lm.server", "--help"],
        ["/staged/bin/python3", "-m", "mlx_vlm.server", "--help"],
        [
            "/staged/bin/python3",
            "-c",
            "from mflux.models.flux.variants.txt2img.flux import Flux1",
        ],
    ]


def test_image_runtime_is_darwin_only_and_locked() -> None:
    node = tomllib.loads(Path("apps/coire-node/pyproject.toml").read_text())
    dependencies = node["project"]["dependencies"]
    assert "mflux==0.20.0; platform_system=='Darwin'" in dependencies

    lock = tomllib.loads(Path("uv.lock").read_text())
    packages = {package["name"]: package for package in lock["package"]}
    assert packages["mflux"]["version"] == "0.20.0"
    assert packages["mflux"]["source"]["registry"] == "https://pypi.org/simple"
    assert any(
        wheel["url"].startswith("https://files.pythonhosted.org/")
        and wheel["hash"].startswith("sha256:")
        for wheel in packages["mflux"]["wheels"]
    )
    node_lock = packages["coire-node"]
    assert any(
        dependency["name"] == "mflux" and dependency.get("marker") == "sys_platform == 'darwin'"
        for dependency in node_lock["dependencies"]
    )
    for core_name in ("coire-api", "coire-agent", "coire-core", "coire-file-worker"):
        core = packages[core_name]
        assert all(dependency["name"] != "mflux" for dependency in core.get("dependencies", []))
    for dockerfile in (
        *Path("apps/coire-api/docker").glob("*.Dockerfile"),
        Path("apps/coire-agent/ops.Dockerfile"),
        Path("apps/coire-file-worker/Dockerfile"),
    ):
        assert "--package coire-node" not in dockerfile.read_text()


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
    stage = _wheel_stage()
    selected = stage.locked_wheels(pylock)
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
