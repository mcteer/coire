"""The actual wheel carries recovery graph verification without editable source imports."""

import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

pytestmark = pytest.mark.integration


def test_built_node_wheel_verifies_graph_from_isolated_interpreter(tmp_path: Path) -> None:
    output = tmp_path / "wheels"
    subprocess.run(
        ["uv", "build", "--package", "coire-node", "--wheel", "--out-dir", str(output)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    wheels = list(output.glob("coire_node-*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as archive:
        assert "coire_node/upgrade_runtime.py" in archive.namelist()
    environment, interpreter = tmp_path / "environment", tmp_path / "interpreter"
    environment.mkdir()
    interpreter.mkdir()
    (environment / "module.py").write_bytes(b"retained-source")
    (interpreter / "python").write_bytes(b"retained-runtime")
    program = (
        "import sys, json; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from coire_node.upgrade_runtime import environment_content_sha256; "
        "import coire_node.upgrade_runtime as runtime; "
        "print(json.dumps({'module': runtime.__file__, 'sha256': "
        "environment_content_sha256(Path(sys.argv[2]), Path(sys.argv[3]))}))"
    )
    result = subprocess.run(
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-B",
            "-c",
            program,
            str(wheels[0]),
            str(environment),
            str(interpreter),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    observed = json.loads(result.stdout)
    assert str(wheels[0]) in observed["module"]
    from coire_node.upgrade_runtime import environment_content_sha256

    assert observed["sha256"] == environment_content_sha256(environment, interpreter)
