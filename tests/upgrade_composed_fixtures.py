"""Trusted preparations from a disposable, actually installed Core cohort."""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from coire_core.models.upgrades import COHORT_ORDER, CoreBootstrapSeed, UpgradeRelease
from coire_core.upgrade_journal import PrivateUpgradeStore


def record_measurement(root: Path, project: str, name: str, value: dict[str, object]) -> None:
    """Retain safe acceptance identities outside rotating pytest directories when requested."""
    payload = json.dumps(value, sort_keys=True)
    local = root / (name + ".json")
    local.write_text(payload)
    local.chmod(0o600)
    destination = os.environ.get("COIRE_UPGRADE_ACCEPTANCE_RESULTS")
    if destination:
        results = PrivateUpgradeStore(Path(destination).resolve())
        retained = results.path(project + "-" + name + ".json")
        retained.write_text(payload)
        retained.chmod(0o600)
        results.sync()


def candidate_image(source: str, *, broken_entrypoint: bool = False) -> tuple[str, str]:
    """Build a label-only candidate, or an explicit failing-entrypoint test image."""
    tag = "coire-upgrade-executor:019-acceptance-" + uuid4().hex[:12]
    source_info = json.loads(
        subprocess.run(
            ["docker", "image", "inspect", source],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )[0]
    reference = "coire-upgrade-executor:019-base-" + uuid4().hex[:12]
    subprocess.run(
        ["docker", "tag", source, reference], capture_output=True, check=True, timeout=10
    )
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix="coire-019-image-") as context:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--network=none",
                    "--pull=false",
                    "--tag",
                    tag,
                    "--file",
                    "-",
                    context,
                ],
                input=(
                    "FROM "
                    + reference
                    + "\nLABEL coire.acceptance="
                    + tag.split(":")[1]
                    + "\n"
                    + (
                        'ENTRYPOINT ["/app/.venv/bin/python3", "-c", '
                        "\"raise RuntimeError('isolated upgrade acceptance failure')\"]\n"
                        if broken_entrypoint
                        else ""
                    )
                ),
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
        candidate = json.loads(
            subprocess.run(
                ["docker", "image", "inspect", tag],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout
        )[0]
        original = {key: value for key, value in source_info["Config"].items() if key != "Labels"}
        changed = {key: value for key, value in candidate["Config"].items() if key != "Labels"}
        if broken_entrypoint:
            assert changed["Entrypoint"] == [
                "/app/.venv/bin/python3",
                "-c",
                "raise RuntimeError('isolated upgrade acceptance failure')",
            ]
            for key in ("Entrypoint", "Cmd"):
                original.pop(key, None)
                changed.pop(key, None)
        assert original == changed
        assert candidate["Architecture"] == "arm64" and candidate["Os"] == "linux"
        assert candidate["RootFS"] == source_info["RootFS"]
        assert candidate["Id"] != source_info["Id"]
        success = True
        return candidate["Id"], tag
    finally:
        subprocess.run(
            ["docker", "image", "rm", reference], capture_output=True, check=True, timeout=10
        )
        if not success:
            subprocess.run(
                ["docker", "image", "rm", tag], capture_output=True, check=False, timeout=10
            )


def prepare_executor_candidate(
    repo: Path,
    root: Path,
    env: dict[str, str],
    frozen: Path,
    client: httpx.Client,
    headers: dict[str, str],
    seed: CoreBootstrapSeed,
    image: str,
) -> UpgradeRelease:
    document = json.loads(frozen.read_text())
    document["services"]["coire-upgrade-executor"]["image"] = image
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    key = "executor-" + digest[:16]
    installed = PrivateUpgradeStore(
        Path(env["COIRE_STATE_ROOT"]) / document["name"] / "releases" / key
    )
    artifact = installed.path("compose.json")
    artifact.write_bytes(payload)
    artifact.chmod(0o600)
    installed.sync()
    manifest = seed.source.manifest.model_copy(
        update={
            "version": "019-isolated-executor",
            "prepared_at": datetime.now(UTC),
            "artifact_sha256s": {"compose.json": digest},
            "compose_sha256": digest,
            "staged_bytes": len(payload),
            "host_release_key": key,
            "compatibility": seed.source.manifest.compatibility.model_copy(
                update={
                    "source_sha256s": (seed.executor_source.manifest_sha256,),
                }
            ),
        }
    )
    target = UpgradeRelease(
        release_id=uuid4(),
        manifest=manifest,
        manifest_sha256=manifest.sha256(),
        prepared_by=seed.source.prepared_by,
        prepared_artifact_id=key,
    )
    preparation = PrivateUpgradeStore(root / "preparation")
    preparation.write("candidate.json", target)
    request = json.loads(
        script(
            repo,
            env,
            "prepare-upgrade",
            str(root / "catalog"),
            "--release-document",
            str(preparation.path("candidate.json")),
            "--artifact",
            "compose.json=" + str(artifact),
            "--publisher-image",
            seed.executor_image_id,
        )
    )
    registered = client.post("/api/v1/admin/upgrades/releases", headers=headers, json=request)
    assert registered.status_code == 201
    return target


def script(repo: Path, env: dict[str, str], *arguments: str) -> str:
    result = subprocess.run(
        ["uv", "run", "python", str(repo / "scripts/coire-deploy.py"), *arguments],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode:
        private_log = Path(arguments[1]).parent / "preparation-stderr.txt"
        private_log.write_text(result.stderr)
        private_log.chmod(0o600)
    assert result.returncode == 0, (
        "trusted composed fixture preparation failed; see private preparation-stderr.txt"
    )
    return result.stdout


def qualify_source(
    repo: Path,
    root: Path,
    env: dict[str, str],
    frozen: Path,
    client: httpx.Client,
    headers: dict[str, str],
    owner: UUID,
) -> CoreBootstrapSeed:
    document = json.loads(frozen.read_text())
    project = document["name"]
    content = frozen.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    services = []
    containers = {}
    for name in COHORT_ORDER:
        if name not in document["services"]:
            continue
        configuration = document["services"][name]
        services.append(
            {
                "service": name,
                "image_id": configuration["image"],
                "configuration": configuration,
                "configuration_sha256": hashlib.sha256(
                    json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        )
        rows = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + project,
                "--filter",
                "label=com.docker.compose.service=" + name,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.split()
        assert len(rows) == 1
        containers[name] = rows[0]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    manifest = {
        "kind": "control_plane",
        "version": "019-isolated-source",
        "source_revision": revision,
        "platform": "linux-arm64",
        "prepared_at": datetime.now(UTC).isoformat(),
        "compatibility": {"source_sha256s": ["f" * 64], "readable_schema_heads": ["0034_upgrades"]},
        "artifact_sha256s": {"compose.json": digest},
        "staged_bytes": len(content),
        "project": project,
        "services": services,
        "schema_head": "0034_upgrades",
        "compose_sha256": digest,
        "host_release_key": frozen.resolve().parent.name,
    }
    from coire_core.models.upgrades import UpgradeReleaseManifest

    parsed = UpgradeReleaseManifest.model_validate(manifest)
    release = UpgradeRelease(
        release_id=uuid4(),
        prepared_by=owner,
        prepared_artifact_id="first-source-" + project,
        manifest=parsed,
        manifest_sha256=parsed.sha256(),
    )
    preparation = PrivateUpgradeStore(root / "preparation")
    preparation.write("source.json", release)
    image = document["services"]["coire-upgrade-executor"]["image"]
    request = json.loads(
        script(
            repo,
            env,
            "prepare-upgrade",
            str(root / "catalog"),
            "--release-document",
            str(preparation.path("source.json")),
            "--artifact",
            "compose.json=" + str(frozen.resolve()),
            "--publisher-image",
            image,
        )
    )
    registered = client.post("/api/v1/admin/upgrades/releases", headers=headers, json=request)
    assert registered.status_code == 201
    executor = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=" + project,
            "--filter",
            "label=com.docker.compose.service=coire-upgrade-executor",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.split()
    assert len(executor) == 1
    seed = CoreBootstrapSeed.model_validate(
        {
            "source": release.model_dump(mode="json"),
            "source_container_ids": containers,
            "executor_runtime_id": "isolated-initial",
            "executor_container_id": executor[0],
            "executor_image_id": image,
            "generation": 1,
            "prepared_at": datetime.now(UTC).isoformat(),
        }
    )
    preparation.write("seed.json", seed)
    accepted = CoreBootstrapSeed.model_validate_json(
        script(repo, env, "prepare-bootstrap", str(preparation.path("seed.json")))
    )
    assert accepted == seed
    return seed
