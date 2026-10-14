"""Native interpreter retention is independent of uv's mutable installation."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def restore_native_test_directory_permissions(tmp_path: Path) -> Iterator[None]:
    yield
    for directory in tmp_path.rglob("*"):
        if not directory.is_symlink() and directory.is_dir():
            directory.chmod(0o700)


def test_native_retained_interpreter_starts_after_original_copy_is_removed(tmp_path: Path) -> None:
    script = Path("apps/coire-node/install_runtime.py")
    spec = importlib.util.spec_from_file_location("native_runtime_installer", script)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    original = Path(sys.base_prefix)
    executable = Path(sys.executable).resolve(strict=True)
    assert executable.is_relative_to(original)
    # Only the isolated test copy may be deleted. Never change uv's real runtime.
    shared_copy = tmp_path / "shared"
    installer.retain_interpreter(original, shared_copy)
    copied_binary = shared_copy / executable.relative_to(original)
    before = hashlib.sha256(copied_binary.read_bytes()).hexdigest()
    selected = installer.prepare_interpreter(
        shared_copy,
        copied_binary,
        tmp_path / "retained",
        prepare_binary=installer.network_python if sys.platform == "darwin" else None,
    )
    assert hashlib.sha256(copied_binary.read_bytes()).hexdigest() == before
    retained = selected.parent.parent
    expected = installer.environment_content_sha256(retained, retained)
    shutil.rmtree(shared_copy)
    result = subprocess.run(
        [
            str(selected),
            "-I",
            "-B",
            "-c",
            "import json, sys, ssl; print(json.dumps({'base': sys.base_prefix, 'executable': sys.executable, 'tls': bool(ssl.OPENSSL_VERSION)}))",
        ],
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert Path(payload["base"]).resolve() == retained
    assert Path(payload["executable"]).resolve() == selected
    assert payload["tls"]
    assert installer.environment_content_sha256(retained, retained) == expected
