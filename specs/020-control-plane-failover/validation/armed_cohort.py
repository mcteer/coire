"""Operator-only armed Core executor handover on Core and both real Studios.

Run from the repository root after loading policy/scanned :020-cohort images and
copying cohort_peer.py plus its matching runtime to /tmp/coire-020-qualification
on each Studio. This runner binds temporary control listeners on port 19460.
It retains private authority/evidence outside Docker; it removes owned containers,
networks and disposable volumes on success. A failed Core cohort is retained for normal recovery.
Application upgrades, native environment replacement and physical power loss
require separate acceptance.
"""

import base64
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fixture_cleanup import retire_completed_project

sys.path.insert(0, "tests")
from upgrade_composed_fixtures import candidate_image, prepare_executor_candidate, qualify_source

from coire_core.failover_crypto import public_key_b64

REPO = Path.cwd()
NAMES = ("coire-core", "coire-edge-a", "coire-edge-b")
PORT = 19460
IMAGE = "coire-failover@sha256:8349e7b70c073b7b67d414d7f3cf6ad1f76a1821fd80321829464ec3f448f23c"


def main() -> None:
    project = "coire-020-cohort-" + uuid4().hex[:10]
    root = Path.home() / ".coire/qualification/020" / project
    root.mkdir(mode=0o700, parents=True)
    (root / "private").mkdir(mode=0o700)
    (root / "results").mkdir(mode=0o700)
    keys = {
        n: base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
        for n in NAMES
    }
    public = {n: public_key_b64(v) for n, v in keys.items()}
    monitor = secrets.token_urlsafe(32)
    address = socket.gethostbyname("coire-core.lab")
    assert address == "192.168.4.10", address
    # Port reservation is a read-only collision check, not permission to replace anything.
    with socket.socket() as probe:
        probe.bind((address, PORT))
    overlay = root / "overlay.yaml"
    overlay.write_text("""services:
      otel-collector:
        ports: !reset []
      coire-api:
        environment:
          IDENTITY_LEGACY_ADMIN_ENABLED: 'true'
          NODE_INVENTORY_FILE: /tmp/no-studio-inventory
          CORE_API_PORT: '19460'
          NODE_LISTEN_PORT: '19460'
          CLOUDFLARE_ACCESS_ISSUER: https://isolated.fixture
          CLOUDFLARE_ACCESS_AUDIENCE: armed-cohort
      coire-upgrade-executor:
        environment:
          CORE_API_PORT: '19460'
    """)
    env = {
        **os.environ,
        "PATH": "/usr/local/bin:/opt/homebrew/bin:" + os.environ["PATH"],
        "COMPOSE_FILE": str(REPO / "deploy/compose/compose.yaml") + os.pathsep + str(overlay),
        "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_PROFILES": "upgrades",
        "COIRE_TAG": "020-cohort",
        "COIRE_REGISTRY": "",
        "COIRE_STATE_ROOT": str(root / "state"),
        "COIRE_SECRETS_BASE": str(root / "private"),
        "COIRE_SECRETS_DIR": str(root / "private"),
        "COIRE_CLUSTER_CONFIG_DIR": str(REPO / "tests/integration/testdata"),
        "COIRE_CONTROL_BIND_ADDRESS": address,
        "COIRE_CONTROL_PORT": str(PORT),
        "COIRE_UPGRADES_ENABLED": "true",
        "COIRE_FAILOVER_MEMBER_NAME": "coire-core",
        "COIRE_FAILOVER_CORE_PUBLIC_KEY": public[NAMES[0]],
        "COIRE_FAILOVER_EDGE_A_PUBLIC_KEY": public[NAMES[1]],
        "COIRE_FAILOVER_EDGE_B_PUBLIC_KEY": public[NAMES[2]],
        "COIRE_SECRET_FAILOVER_PEER_KEY": keys[NAMES[0]],
        "COIRE_SECRET_FAILOVER_MONITOR_TOKEN": monitor,
        "COIRE_SECRET_POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "COIRE_SECRET_KEY_SIGNING_SECRET": secrets.token_urlsafe(32),
        "COIRE_SECRET_ADMIN_TOKEN": "bootstrap-" + secrets.token_urlsafe(32),
        "COIRE_SECRET_BOOTSTRAP_ADMIN_EMAIL": "bootstrap@isolated.test",
        "COIRE_SECRET_NODE_TOKENS": "{}",
        "COIRE_SECRET_OPS_SERVICE_TOKEN": "coire_ops_" + secrets.token_hex(32),
        "COIRE_SECRET_FILE_WORKER_SERVICE_TOKEN": "coire_file_worker_" + secrets.token_hex(32),
        "COIRE_SECRET_UPGRADE_EXECUTOR_TOKEN": "coire_upgrade_" + secrets.token_hex(32),
    }
    peers: dict[str, subprocess.Popen[bytes]] = {}
    logs: list[BinaryIO] = []
    compose: list[str] | None = None
    candidate_tags: list[str] = []
    evidence: list[dict[str, Any]] = []
    completed = False

    def save(name: str, value: object) -> None:
        p = root / "results" / (name + ".json")
        p.write_text(json.dumps(value, indent=2) + "\n")
        p.chmod(0o600)

    def receive(p: subprocess.Popen[bytes]) -> dict[str, Any]:
        assert p.stdout is not None
        assert select.select([p.stdout], [], [], 30)[0], "native observer timed out"
        line = p.stdout.readline()
        assert line, "native observer exited"
        return cast(dict[str, Any], json.loads(line))

    def sample(client: httpx.Client) -> dict[str, Any]:
        values: dict[str, dict[str, Any]] = {}
        for p in peers.values():
            assert p.stdin is not None
            p.stdin.write(b'{"op":"sample"}\n')
            p.stdin.flush()
        for name, p in peers.items():
            values[name] = receive(p)
        try:
            ready = (
                client.get(
                    "/failover/ready", headers={"X-Coire-Failover-Monitor": monitor}
                ).status_code
                == 200
            )
        except httpx.TransportError:
            ready = False
        assert sum(bool(v["serving"]) for v in values.values()) + int(ready) <= 1, (
            "overlapping admitted origins"
        )
        assert all(v["engine_count"] == 0 for v in values.values())
        row = {"at": datetime.now(UTC).isoformat(), "core_ready": ready, "peers": values}
        evidence.append(row)
        save("observations", evidence)
        return row

    def wait_healthy(client: httpx.Client, timeout: float = 390) -> None:
        deadline = time.monotonic() + timeout
        while True:
            row = sample(client)
            if row["core_ready"] and all(v["role"] == "standby" for v in row["peers"].values()):
                return
            assert time.monotonic() < deadline, "armed cohort did not settle"
            time.sleep(2)

    try:
        for name in NAMES[1:]:
            log = (root / "private" / (name + ".log")).open("wb")
            logs.append(log)
            p = subprocess.Popen(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    name + ".lab",
                    "cd /tmp/coire-020-qualification && .venv/bin/python specs/020-control-plane-failover/validation/cohort_peer.py",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
            )
            peers[name] = p
            assert p.stdin is not None
            p.stdin.write(
                json.dumps(
                    {
                        "name": name,
                        "root": "/tmp/" + project,
                        "key": keys[name],
                        "core_public_key": public[NAMES[0]],
                        "port": PORT,
                        "image": IMAGE,
                    }
                ).encode()
                + b"\n"
            )
            p.stdin.flush()
            assert receive(p)["ready"]
        with (root / "private/install.log").open("wb") as log:
            subprocess.run(
                [str(REPO / "deploy/compose/coire-up"), "--secrets-from-env", "--no-build"],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=240,
            )
        frozen = root / "state" / project / "current/compose.json"
        compose = ["docker", "compose", "-p", project, "-f", str(frozen)]
        with httpx.Client(
            base_url=f"http://{address}:{PORT}", timeout=5, trust_env=False
        ) as client:
            headers = {"Authorization": "Bearer " + env["COIRE_SECRET_ADMIN_TOKEN"]}
            user = client.post(
                "/api/v1/admin/users",
                headers=headers,
                json={
                    "email": "upgrade-admin@isolated.test",
                    "display_name": "Armed cohort acceptance",
                    "role": "admin",
                },
            )
            assert user.status_code == 201, user.status_code
            issued = client.post(
                "/api/v1/admin/users/" + user.json()["id"] + "/keys",
                headers=headers,
                json={
                    "name": "armed-cohort",
                    "scopes": ["admin"],
                    "requests_per_minute": 10000,
                    "monthly_budget_tokens": 10000,
                },
            )
            assert issued.status_code == 201, issued.status_code
            headers = {"Authorization": "Bearer " + issued.json()["secret"]}
            seed = qualify_source(REPO, root, env, frozen, client, headers, UUID(user.json()["id"]))
            wait_healthy(client)
            save(
                "source",
                {
                    "source": seed.source.manifest_sha256,
                    "project": project,
                    "executor_image": seed.executor_image_id,
                    "runtime_source": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], text=True
                    ).strip(),
                },
            )
            print("Armed Core SQL cohort ready; native peers standby", flush=True)
            for failed in (True, False):
                image, tag = candidate_image(seed.executor_image_id)
                candidate_tags.append(tag)
                target = prepare_executor_candidate(
                    REPO, root, env, frozen, client, headers, seed, image
                )
                response = client.post(
                    "/api/v1/admin/upgrades/executor-handover",
                    headers=headers,
                    json={
                        "release_id": str(target.release_id),
                        "expected_incumbent_seed_sha256": seed.sha256(),
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert response.status_code == 202, (response.status_code, response.text[:500])
                accepted = response.json()
                started = time.monotonic()
                deadline = started + (420 if failed else 270)
                stopped = False
                phases: list[str] = []
                while True:
                    response = client.get(
                        "/api/v1/admin/upgrades/executor-handover/" + accepted["handover_id"],
                        headers=headers,
                    )
                    assert response.status_code == 200, response.status_code
                    current = response.json()
                    if not phases or current["phase"] != phases[-1]:
                        phases.append(current["phase"])
                    owner = json.loads(
                        (
                            root / "state" / project / "upgrades/coordination/ownership.json"
                        ).read_text()
                    )
                    if (
                        failed
                        and not stopped
                        and current["phase"] in {"transfer_consumed", "claimed"}
                        and owner["state"] == "active"
                        and owner["generation"] == accepted["generation"]
                        and owner["executor_container_id"] != seed.executor_container_id
                    ):
                        identity = owner["executor_container_id"]
                        actual = json.loads(
                            subprocess.check_output(["docker", "inspect", identity])
                        )[0]
                        assert (
                            actual["Image"] == image
                            and actual["Config"]["Labels"]["com.docker.compose.project"] == project
                        )
                        subprocess.run(
                            ["docker", "stop", "--time=5", identity],
                            stdout=subprocess.DEVNULL,
                            check=True,
                            timeout=15,
                        )
                        stopped = True
                    sample(client)
                    terminal = "restored" if failed else "committed"
                    if current["phase"] == terminal:
                        break
                    assert current["phase"] not in {"restored", "committed", "failed_unchanged"}, (
                        current["phase"]
                    )
                    assert time.monotonic() < deadline, "handover exceeded bound"
                    time.sleep(0.5)
                assert not failed or stopped
                wait_healthy(client, 90)
                save(
                    "failed-candidate" if failed else "successful-handover",
                    {
                        "handover_id": accepted["handover_id"],
                        "phase": current["phase"],
                        "phases": phases,
                        "seconds": time.monotonic() - started,
                        "scope": "actual armed executor handover; no application upgrade or native environment replacement",
                    },
                )
                print(
                    "candidate restoration passed" if failed else "executor handover passed",
                    flush=True,
                )
            completed = True
    finally:
        # Preserve every observer/log and avoid deleting private release/authority evidence.
        cleanup_errors: list[Exception] = []
        try:
            if compose:
                subprocess.run(
                    [*compose, "logs", "--no-color"],
                    stdout=(root / "private/cohort.log").open("wb"),
                    stderr=subprocess.STDOUT,
                    timeout=30,
                )
                if completed:
                    subprocess.run(
                        [*compose, "down", "--remove-orphans"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=90,
                        check=True,
                    )
                    owned = subprocess.check_output(
                        [
                            "docker",
                            "ps",
                            "-aq",
                            "--no-trunc",
                            "--filter",
                            "label=com.docker.compose.project=" + project,
                        ],
                        text=True,
                    ).split()
                    for identity in owned:
                        actual = json.loads(
                            subprocess.check_output(["docker", "inspect", identity])
                        )[0]
                        assert actual["Config"]["Labels"]["com.docker.compose.project"] == project
                        assert (
                            actual["Config"]["Labels"]["com.docker.compose.service"]
                            == "coire-upgrade-executor"
                        )
                        with (root / "private" / ("executor-" + identity[:12] + ".log")).open(
                            "wb"
                        ) as log:
                            subprocess.run(
                                ["docker", "logs", identity],
                                stdout=log,
                                stderr=subprocess.STDOUT,
                                check=True,
                                timeout=20,
                            )
                        if actual["State"]["Running"]:
                            subprocess.run(
                                ["docker", "stop", "--time=10", identity],
                                capture_output=True,
                                check=True,
                                timeout=20,
                            )
                        subprocess.run(
                            ["docker", "rm", identity], capture_output=True, check=True, timeout=10
                        )
                    networks = subprocess.check_output(
                        [
                            "docker",
                            "network",
                            "ls",
                            "-q",
                            "--filter",
                            "label=com.docker.compose.project=" + project,
                        ],
                        text=True,
                    ).split()
                    for identity in networks:
                        actual = json.loads(
                            subprocess.check_output(["docker", "network", "inspect", identity])
                        )[0]
                        assert (
                            actual["Labels"]["com.docker.compose.project"] == project
                            and not actual["Containers"]
                        )
                        subprocess.run(
                            ["docker", "network", "rm", identity],
                            capture_output=True,
                            check=True,
                            timeout=10,
                        )
                    retire_completed_project(project, terminal=True)
        except Exception as exc:
            cleanup_errors.append(exc)
        for p in peers.values():
            try:
                if p.poll() is None:
                    assert p.stdin is not None
                    p.stdin.write(b'{"op":"exit"}\n')
                    p.stdin.flush()
                    p.wait(timeout=30)
                assert p.returncode == 0, "native peer cleanup failed"
            except Exception as exc:
                cleanup_errors.append(exc)
            finally:
                if p.stdin is not None:
                    p.stdin.close()
        for retained_log in logs:
            retained_log.close()
        if completed and not cleanup_errors:
            for tag in candidate_tags:
                try:
                    subprocess.run(
                        ["docker", "image", "rm", tag], capture_output=True, check=True, timeout=15
                    )
                except Exception as exc:
                    cleanup_errors.append(exc)
        save(
            "outcome",
            {
                "completed": completed,
                "cleanup_completed": completed and not cleanup_errors,
                "root": str(root),
                "project": project,
                "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            },
        )
        print("Private cohort result:", root, flush=True)
        if cleanup_errors:
            raise ExceptionGroup(
                "Qualification cleanup incomplete; private evidence retained", cleanup_errors
            )


if __name__ == "__main__":
    main()
