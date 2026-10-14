"""Build honest, hash-verifiable candidate wheels for isolated native CI.

Healthy candidates add a startup log. Broken candidates actually fail startup;
their wheel RECORD hashes describe the modified bytes before installation.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from zipfile import ZipFile

from integration.provision_upgrade_ci_vm import require_hosted_mac

from coire_core.models.upgrades import (
    PreparedNativeRuntime,
    UpgradeCompatibility,
    UpgradeKind,
    UpgradeRelease,
    UpgradeReleaseManifest,
    UpgradeReleaseRegister,
)
from coire_core.upgrade_artifacts import publish_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_runtime import verify_prepared_runtime


def candidate_wheel(source: Path, output: Path, kind: Literal["healthy", "broken"]) -> None:
    if output.exists():
        raise RuntimeError("Native candidate output must be fresh")
    with ZipFile(source) as wheel:
        entries = {info.filename: (info, wheel.read(info)) for info in wheel.infolist()}
    name = "coire_node/__main__.py"
    entry, content = entries[name]
    anchor = b"    settings = Settings()\n"
    if content.count(anchor) != 1:
        raise RuntimeError("Native candidate entrypoint differs from the reviewed startup")
    effect = (
        b'    logger.info("isolated native CI candidate startup")\n'
        if kind == "healthy"
        else b'    raise RuntimeError("isolated native CI broken candidate")\n'
    )
    entries[name] = entry, content.replace(anchor, effect + anchor)
    records = [name for name in entries if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise RuntimeError("Native wheel must have one RECORD")
    record = records[0]
    rows = io.StringIO(newline="")
    writer = csv.writer(rows, lineterminator="\n")
    for name, (_, content) in sorted(entries.items()):
        if name == record:
            writer.writerow([name, "", ""])
        else:
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            writer.writerow([name, "sha256=" + digest.decode(), str(len(content))])
    entries[record] = entries[record][0], rows.getvalue().encode()
    with output.open("xb") as destination:
        output.chmod(0o600)
        with ZipFile(destination, "w") as wheel:
            for info, content in entries.values():
                wheel.writestr(info, content)


def install_stage(repo: Path, native_root: Path, wheel_directory: Path) -> None:
    """Use the ordinary installer without selecting a runtime or creating launchd jobs."""
    require_hosted_mac(native_root)
    subprocess.run(
        [
            str(repo / "apps/coire-node/install.sh"),
            "--prefix",
            str(native_root),
            "--wheel-dir",
            str(wheel_directory),
            "--stage-only",
        ],
        cwd=repo,
        check=True,
        timeout=1200,
    )


def install_initial_source(repo: Path, native_root: Path, wheel_directory: Path) -> None:
    """Select only a first Source through the ordinary installer, before bootstrap.

    The installer renders launchd instructions but does not install or start
    global services. The private CI supervisor owns the subsequent processes.
    """
    require_hosted_mac(native_root)
    current = native_root / "envs/current"
    bootstrap = native_root / "upgrade-bootstrap"
    if current.exists() or current.is_symlink() or bootstrap.exists() or bootstrap.is_symlink():
        raise RuntimeError("CI initial install cannot replace an existing Source or bootstrap")
    subprocess.run(
        [
            str(repo / "apps/coire-node/install.sh"),
            "--prefix",
            str(native_root),
            "--wheel-dir",
            str(wheel_directory),
        ],
        cwd=repo,
        check=True,
        timeout=1200,
    )


def initialize_source_bootstrap(
    repo: Path,
    native_root: Path,
    node: Literal["coire-edge-a", "coire-edge-b"],
    request: UpgradeReleaseRegister,
) -> None:
    """Use the ordinary first-install bootstrap producer on the hosted fixture.

    The installer verifies the selected runtime and prepared Source bytes. It
    renders launchd instructions; the private CI supervisor subsequently owns
    processes. This routine neither writes authority itself nor enables launchd.
    """
    require_hosted_mac(native_root)
    if (
        native_root.resolve(strict=True) != native_root
        or native_root.stat().st_uid != os.getuid()
        or native_root.stat().st_mode & 0o077
    ):
        raise RuntimeError("CI bootstrap root must be private, unlinked and owned")
    if node not in {"coire-edge-a", "coire-edge-b"}:
        raise RuntimeError("Native CI bootstrap requires one declared fixture node")
    bootstrap = native_root / "upgrade-bootstrap"
    if bootstrap.exists() or bootstrap.is_symlink():
        raise RuntimeError("CI initial bootstrap cannot replace existing recovery authority")
    records = PrivateUpgradeStore(native_root / "fixture-records")
    command = records.path("initial-bootstrap-request.json")
    if command.exists() or command.is_symlink():
        raise RuntimeError("CI initial bootstrap request is already retained")
    records.write("initial-bootstrap-request.json", request, replace=False)
    # The production installer deliberately derives a physical Studio hostname.
    # Hosted CI has two logical nodes on one ephemeral Mac. Invoke the same
    # installed bootstrap producer directly after hosted isolation validation;
    # it verifies the prepared Source and publishes only under this private root.
    with command.open("rb") as body:
        subprocess.run(
            [
                str(native_root / "envs/current/bin/python3"),
                "-I",
                "-B",
                "-m",
                "coire_node.upgrade_bootstrap",
            ],
            stdin=body,
            cwd=repo,
            env={
                **os.environ,
                "NODE_NAME": node,
                "UPGRADE_ENVIRONMENT_ROOT": str(native_root / "envs"),
                "UPGRADE_BOOTSTRAP_ROOT": str(bootstrap),
                "UPGRADE_PREPARED_DIR": str(native_root / "upgrades/prepared"),
            },
            check=True,
            timeout=120,
        )


def prepare_release(
    native_root: Path,
    wheel_directory: Path,
    *,
    environment_id: str,
    owner: UUID,
    revision: str,
    compatible_sources: tuple[str, ...],
    label: str,
) -> tuple[UpgradeRelease, UpgradeReleaseRegister]:
    """Publish verified installed bytes through the ordinary prepared artifact path."""
    require_hosted_mac(native_root)
    identities = PrivateUpgradeStore(native_root / "envs/.identities")
    runtime = identities.read(environment_id + ".json", PreparedNativeRuntime)
    if runtime is None:
        raise RuntimeError("CI native runtime has no actual installer identity")
    verify_prepared_runtime(native_root / "envs", native_root / "runtimes", runtime)
    paths = {}
    for package, name in (("coire_core", "coire-core.whl"), ("coire_node", "coire-node.whl")):
        matches = list(wheel_directory.glob(package + "-*.whl"))
        if len(matches) != 1:
            raise RuntimeError("CI release requires exactly one core and node wheel")
        paths[name] = matches[0]
    manifest = UpgradeReleaseManifest(
        kind=UpgradeKind.STUDIO,
        version="0.2.0-ci-" + label,
        source_revision=revision,
        platform="macos-arm64",
        prepared_at=datetime.now(UTC),
        compatibility=UpgradeCompatibility(
            source_sha256s=compatible_sources, readable_schema_heads=("0034_upgrades",)
        ),
        artifact_sha256s={
            name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()
        },
        staged_bytes=sum(path.stat().st_size for path in paths.values()),
        environment_id=runtime.environment_id,
        environment_sha256=runtime.environment_sha256,
        paired_required=False,
    )
    release = UpgradeRelease(
        release_id=uuid4(),
        prepared_by=owner,
        prepared_artifact_id="native-ci-" + label,
        manifest=manifest,
        manifest_sha256=manifest.sha256(),
    )
    request = publish_prepared_release(native_root / "upgrades/prepared", release, paths)
    return release, request
