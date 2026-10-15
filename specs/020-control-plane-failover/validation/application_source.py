"""Install a fresh private native Source with the ordinary locked installer.

A failed cohort's held native prefix is never reused. This creates only a new
installation and bootstrap; the application supervisor starts its real services.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from coire_core.models.upgrades import (
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
    name = initial["node"]
    assert name in {"coire-edge-a", "coire-edge-b"}
    assert socket.gethostname().split(".")[0] == name
    root = Path(initial["root"])
    assert root.parent == Path.home() / ".coire/qualification/020"
    assert root.name.startswith("coire-020-application-")
    assert not root.exists() and not root.is_symlink()
    root.mkdir(mode=0o700)
    assert root.resolve(strict=True) == root
    wheels = Path.cwd() / "dist"
    artifacts = {}
    for package, key in (("coire_core", "coire-core.whl"), ("coire_node", "coire-node.whl")):
        (path,) = wheels.glob(package + "-*.whl")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == initial["wheel_sha256s"][key]
        artifacts[key] = path
    log_path = root / "installation.log"
    with log_path.open("xb") as log:
        log_path.chmod(0o600)
        subprocess.run(
            [
                str(Path.cwd() / "apps/coire-node/install.sh"),
                "--prefix",
                str(root),
                "--wheel-dir",
                str(wheels),
            ],
            env={
                **os.environ,
                "PATH": str(Path.home() / ".local/bin") + ":" + os.environ["PATH"],
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1200,
        )
    environment = (root / "envs/current").resolve(strict=True)
    runtime = PrivateUpgradeStore(root / "envs/.identities", create=False).read(
        environment.name + ".json", PreparedNativeRuntime
    )
    assert runtime is not None
    python = verify_prepared_runtime(root / "envs", root / "runtimes", runtime)
    manifest = UpgradeReleaseManifest(
        kind=UpgradeKind.STUDIO,
        platform="macos-arm64",
        version="0.2.0-020-fresh-source",
        source_revision=initial["revision"],
        prepared_at=datetime.now(UTC),
        compatibility=UpgradeCompatibility(
            source_sha256s=(initial["compatible_source"],),
            readable_schema_heads=("0034_upgrades",),
        ),
        artifact_sha256s=initial["wheel_sha256s"],
        staged_bytes=sum(path.stat().st_size for path in artifacts.values()),
        environment_id=runtime.environment_id,
        environment_sha256=runtime.environment_sha256,
    )
    release = UpgradeRelease(
        release_id=uuid4(),
        prepared_by=UUID(initial["owner"]),
        prepared_artifact_id="020-fresh-source-" + root.name.removeprefix("coire-020-application-"),
        manifest=manifest,
        manifest_sha256=manifest.sha256(),
    )
    request = publish_prepared_release(root / "upgrades/prepared", release, artifacts)
    bootstrap_log = root / "bootstrap.log"
    with bootstrap_log.open("xb") as log:
        bootstrap_log.chmod(0o600)
        subprocess.run(
            [str(python), "-I", "-B", "-m", "coire_node.upgrade_bootstrap"],
            input=request.model_dump_json().encode(),
            env={
                **os.environ,
                "NODE_NAME": name,
                "UPGRADE_ENVIRONMENT_ROOT": str(root / "envs"),
                "UPGRADE_BOOTSTRAP_ROOT": str(root / "upgrade-bootstrap"),
                "UPGRADE_PREPARED_DIR": str(root / "upgrades/prepared"),
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=120,
        )
    print(json.dumps({"root": str(root), "source": release.model_dump(mode="json")}))


if __name__ == "__main__":
    main()
