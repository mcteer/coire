#!/usr/bin/env python3
"""Operator-root activation of a verified node candidate, preserving installed settings."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from coire_core.models.training_node import NodeTrainingLeaseSnapshot
from coire_core.settings import Settings

PLIST = Path("/Library/LaunchDaemons/com.coire.node.plist")
PREFIX = Path("/opt/coire")


def validate_candidate_runtime(candidate: Path, *, prefix: Path = PREFIX) -> Path:
    """Reject legacy/shared executable bindings before service or active-link changes."""
    executable = (candidate / "bin/python3").resolve(strict=True)
    if (
        not executable.is_file()
        or executable.name != "coire-node-python"
        or executable.parent.name != "bin"
        or executable.parents[2] != prefix / "python"
    ):
        raise ValueError("candidate must bind the dedicated runtime under /opt/coire/python")
    return executable


def checked(argv: list[str]) -> None:
    subprocess.run(argv, check=True, timeout=60, stdout=subprocess.DEVNULL)


def replacement(
    original: dict[str, object],
    node: str,
    *,
    agent_image: str | None = None,
    relay_image: str | None = None,
) -> dict[str, object]:
    if original.get("Label") != "com.coire.node" or original.get("AbandonProcessGroup") is not True:
        raise ValueError("installed service identity/engine survival policy differs")
    if original.get("ProgramArguments") != [
        "/opt/coire/envs/current/bin/python3",
        "-m",
        "coire_node",
    ]:
        raise ValueError("installed service does not use the expected versioned interpreter")
    environment = original.get("EnvironmentVariables")
    if not isinstance(environment, dict) or environment.get("NODE_NAME") != node:
        raise ValueError("installed service node identity differs")
    changed = dict(original)
    proposed_environment = {
        **environment,
        "TRAINING_ENABLED": "true",
        "TRAINING_INPUT_API_URL": "http://coire-core.lab:8180",
    }
    if agent_image is not None or relay_image is not None:
        if not agent_image or not relay_image:
            raise ValueError("both nonempty harness image digests are required")
        proposed_environment.update(
            {
                "RUN_AGENT_IMAGE": Settings.run_images_are_digest_pinned(agent_image),
                "RUN_RELAY_IMAGE": Settings.run_images_are_digest_pinned(relay_image),
            }
        )
    changed["EnvironmentVariables"] = proposed_environment
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--agent-image", help="preloaded digest-pinned user harness image")
    parser.add_argument("--relay-image", help="preloaded digest-pinned run relay image")
    args = parser.parse_args()
    node = platform.node().split(".", 1)[0]
    candidate = args.candidate
    if node not in {"coire-edge-a", "coire-edge-b"} or platform.system() != "Darwin":
        parser.error("run on a declared Studio")
    if (
        candidate.parent != PREFIX / "envs"
        or candidate.is_symlink()
        or not (candidate / "bin/python3").is_file()
    ):
        parser.error("candidate must be an existing immutable /opt/coire/envs generation")
    runtime = validate_candidate_runtime(candidate)
    original_bytes = PLIST.read_bytes()
    original = plistlib.loads(original_bytes)
    proposed = replacement(
        original, node, agent_image=args.agent_image, relay_image=args.relay_image
    )
    hostfile = Path("/Users/mcteer/coire-stage/016-jaccl-hostfile.json")
    if hostfile.is_symlink() or not hostfile.is_file() or hostfile.stat().st_size > 65536:
        raise ValueError("staged native JACCL inventory is unavailable")
    inventory = json.loads(hostfile.read_bytes())
    if inventory.get("backend") != "jaccl" or [entry["ssh"] for entry in inventory["hosts"]] != [
        "coire-edge-a.fabric",
        "coire-edge-b.fabric",
    ]:
        raise ValueError("staged JACCL inventory is not the declared Studio pair")
    if not args.apply:
        print(
            json.dumps(
                {
                    "node": node,
                    "candidate": str(candidate),
                    "runtime": str(runtime),
                    "preserved_service_settings": True,
                    "root_required": True,
                    "core_compatibility_preflight_required": True,
                }
            )
        )
        return
    if os.geteuid() != 0:
        parser.error("operator-authenticated root is required")
    token = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-w",
            "-s",
            "coire-node-token",
            "/Library/Keychains/System.keychain",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        response = client.get(
            f"http://coire-core.lab:8180/api/v1/internal/training/nodes/{node}/leases",
            headers={"Authorization": "Bearer " + token, "X-Coire-Node": node},
        )
        response.raise_for_status()
        snapshot = NodeTrainingLeaseSnapshot.model_validate_json(response.content)
        if snapshot.node != node:
            raise ValueError("core compatibility preflight returned another node")
    checked(
        [
            str(candidate / "bin/python3"),
            "-c",
            "import coire_node.training.retention,coire_node.training.worker; from coire_core.models.training_node import TrainingAttemptCleanupRequest",
        ]
    )
    backup = Path(tempfile.mkdtemp(prefix="node-activation-", dir=PREFIX / "state"))
    backup.chmod(0o700)
    (backup / "service.plist").write_bytes(original_bytes)
    current = PREFIX / "envs/current"
    old = current.resolve(strict=True)
    (backup / "previous.json").write_text(
        json.dumps({"previous": str(old), "candidate": str(candidate)})
    )
    staged = backup / "proposed.plist"
    staged.write_bytes(plistlib.dumps(proposed))
    staged.chmod(0o644)
    if PLIST.read_bytes() != original_bytes:
        raise ValueError("installed service changed concurrently")
    cluster_target = PREFIX / "state/jaccl-hostfile.json"
    if cluster_target.is_symlink():
        raise ValueError("installed JACCL inventory is linked")
    if cluster_target.exists():
        shutil.copy2(cluster_target, backup / "jaccl-hostfile.json")
    stopped = False
    try:
        checked(["/bin/launchctl", "bootout", "system/com.coire.node"])
        stopped = True
        for _ in range(30):
            state = subprocess.run(
                ["/bin/launchctl", "print", "system/com.coire.node"], capture_output=True, timeout=5
            )
            if state.returncode:
                break
            time.sleep(1)
        else:
            raise RuntimeError("prior node registration remains active")
        pending = current.with_name("current.training.pending")
        pending.symlink_to(candidate)
        os.replace(pending, current)
        generated = backup / "generated-jaccl-hostfile.json"
        generated.write_bytes(hostfile.read_bytes())
        generated.chmod(0o644)
        os.replace(generated, cluster_target)
        os.replace(staged, PLIST)
        checked(["/bin/launchctl", "bootstrap", "system", str(PLIST)])
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            for _ in range(60):
                try:
                    health = client.get(
                        f"http://{node}.lab:9400/node/health",
                        headers={"Authorization": "Bearer " + token},
                    )
                    reconcile = client.post(
                        f"http://{node}.lab:9400/node/training/reconcile",
                        headers={"Authorization": "Bearer " + token},
                        json={"expected": []},
                    )
                    if health.status_code == 200 and reconcile.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError(
                    "activated node did not pass authenticated health/training smoke"
                )
    except BaseException:
        if stopped:
            subprocess.run(
                ["/bin/launchctl", "bootout", "system/com.coire.node"],
                capture_output=True,
                timeout=30,
            )
            restore = backup / "restore.plist"
            restore.write_bytes(original_bytes)
            restore.chmod(0o644)
            os.replace(restore, PLIST)
            pending = current.with_name("current.training.rollback")
            pending.symlink_to(old)
            os.replace(pending, current)
            cluster_target = PREFIX / "state/jaccl-hostfile.json"
            previous_hostfile = backup / "jaccl-hostfile.json"
            if previous_hostfile.exists():
                os.replace(previous_hostfile, cluster_target)
            else:
                cluster_target.unlink(missing_ok=True)
            checked(["/bin/launchctl", "bootstrap", "system", str(PLIST)])
        raise
    print(
        json.dumps(
            {
                "node": node,
                "candidate": str(candidate),
                "activated": True,
                "backup": str(backup),
                "authenticated_health_verification_required": True,
            }
        )
    )


if __name__ == "__main__":
    main()
