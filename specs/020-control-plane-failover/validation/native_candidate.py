"""Stage a real immutable native candidate; never select it outside the admin API."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, "tests")
from integration.upgrade_native_ci_wheels import candidate_wheel

from coire_core.models.upgrades import (
    NativeNodeProcess,
    PreparedNativeRuntime,
    UpgradeCompatibility,
    UpgradeKind,
    UpgradeRelease,
    UpgradeReleaseManifest,
)
from coire_core.upgrade_artifacts import publish_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_runtime import verify_prepared_runtime


def main() -> None:
    initial = json.loads(sys.stdin.readline())
    root = Path(initial["root"])
    assert root.resolve(strict=True) == root
    assert root.parent == Path.home() / ".coire/qualification/020"
    assert root.name.startswith("coire-020-application-")
    assert root.stat().st_uid == os.getuid() and root.stat().st_mode & 0o077 == 0
    assert initial["kind"] in {"healthy", "broken"}
    source = UpgradeRelease.model_validate(initial["source"])
    state = Path(initial["state"])
    assert state == root / ("state-" + initial["cohort"])
    current = PrivateUpgradeStore(state / "upgrades", create=False).read(
        "node-process.json", NativeNodeProcess
    )
    assert current is not None and current.selected.release_sha256 == source.manifest_sha256
    assert current.selected.runtime.environment_sha256 == source.manifest.environment_sha256
    source_python = verify_prepared_runtime(
        root / "envs", root / "runtimes", current.selected.runtime
    )
    base = Path.cwd() / "dist"
    (core,) = base.glob("coire_core-*.whl")
    (node,) = base.glob("coire_node-*.whl")
    assert (
        hashlib.sha256(core.read_bytes()).hexdigest()
        == source.manifest.artifact_sha256s["coire-core.whl"]
    )
    assert (
        hashlib.sha256(node.read_bytes()).hexdigest()
        == source.manifest.artifact_sha256s["coire-node.whl"]
    )
    stage = root / "candidates" / initial["trial"]
    stage.mkdir(mode=0o700, parents=True)
    (stage / "node-wheels").symlink_to(base / "node-wheels", target_is_directory=True)
    core_copy = stage / core.name
    shutil.copyfile(core, core_copy)
    candidate = stage / node.name
    candidate_wheel(node, candidate, initial["kind"])
    log_path = stage / "installation.log"
    with log_path.open("xb") as log:
        log_path.chmod(0o600)
        subprocess.run(
            [
                str(Path.cwd() / "apps/coire-node/install.sh"),
                "--prefix",
                str(root),
                "--wheel-dir",
                str(stage),
                "--stage-only",
            ],
            env={
                **os.environ,
                "PATH": str(Path.home() / ".local/bin") + ":" + os.environ["PATH"],
                "UV_OFFLINE": "1",
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1200,
        )
    interpreter = root / "runtimes" / current.selected.runtime.interpreter_id
    python = interpreter / "bin/coire-node-python"
    identity = subprocess.check_output(
        [
            str(source_python),
            "apps/coire-node/install_runtime.py",
            "--runtime-input-id",
            str(interpreter),
            str(stage / "node-wheels/requirements.txt"),
            str(core_copy),
            str(candidate),
            str(python),
        ],
        text=True,
        timeout=60,
    ).strip()
    environment_id = "0.2.0-" + identity
    runtime = PrivateUpgradeStore(root / "envs/.identities", create=False).read(
        environment_id + ".json", PreparedNativeRuntime
    )
    assert runtime is not None and runtime != current.selected.runtime
    verify_prepared_runtime(root / "envs", root / "runtimes", runtime)
    assert (root / "envs/current").resolve().name == current.selected.runtime.environment_id
    files = {"coire-core.whl": core_copy, "coire-node.whl": candidate}
    manifest = UpgradeReleaseManifest(
        kind=UpgradeKind.STUDIO,
        platform="macos-arm64",
        version="0.2.0-020-" + initial["kind"],
        source_revision=source.manifest.source_revision,
        prepared_at=datetime.now(UTC),
        compatibility=UpgradeCompatibility(
            source_sha256s=(source.manifest_sha256,), readable_schema_heads=("0034_upgrades",)
        ),
        artifact_sha256s={
            key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in files.items()
        },
        staged_bytes=sum(path.stat().st_size for path in files.values()),
        environment_id=runtime.environment_id,
        environment_sha256=runtime.environment_sha256,
    )
    release = UpgradeRelease(
        release_id=uuid4(),
        prepared_by=UUID(initial["owner"]),
        prepared_artifact_id="020-native-" + initial["trial"],
        manifest=manifest,
        manifest_sha256=manifest.sha256(),
    )
    request = publish_prepared_release(root / "upgrades/prepared", release, files)
    print(
        json.dumps(
            {
                "release": release.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
                "files": {key: str(path) for key, path in files.items()},
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
