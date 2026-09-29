"""A failed node smoke keeps the previous versioned runtime active."""

from __future__ import annotations

import importlib.util
import io
import subprocess
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
        ["/staged/bin/python3", "-c", "import coire_core, coire_node, mlx_lm, mlx_vlm"],
        ["/staged/bin/python3", "-m", "mlx_lm.server", "--help"],
        ["/staged/bin/python3", "-m", "mlx_vlm.server", "--help"],
    ]


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
