"""Shared fixtures for the integration suite.

The stack is brought up **once per session**, not per module. It was previously created by a
module-scoped fixture in test_bringup.py, which tore it down when that module finished — so
test_restart_isolation.py, which only asserts the stack is healthy, ran against nothing and
spent 90s per test waiting for a control plane that no longer existed. Running the two files
separately hid it; running the suite as CI does exposed it.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

REPO = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO / "deploy/compose"
UP = COMPOSE_DIR / "coire-up"
DOWN = COMPOSE_DIR / "coire-down"

# The integration suite runs in its own compose project so it never shares a Postgres volume
# with a developer's running stack. Postgres sets its password only on first init, so a volume
# created with different credentials makes coire-migrate fail to authenticate — which is
# exactly what happened locally while CI, always starting fresh, stayed green.
PROJECT = "coire-it"
os.environ.setdefault("COMPOSE_PROJECT_NAME", PROJECT)

# `coire-up` runs `docker compose up` from deploy/compose and relies on auto-discovery, so the
# integration overlay is selected with COMPOSE_FILE rather than by teaching the script a flag
# it would only ever use here. Docker Compose reads it as a path-separated list.
os.environ.setdefault("COMPOSE_FILE", f"compose.yaml{os.pathsep}compose.override.it.yaml")
# Production publishes nginx on 8180 so it can coexist with other local control planes. The
# integration contract predates that host-port choice and all live fixtures intentionally use
# loopback 8080; pin the test project explicitly instead of inheriting deploy/compose/.env.
os.environ.setdefault("COIRE_CONTROL_BIND_ADDRESS", "127.0.0.1")
os.environ.setdefault("COIRE_CONTROL_PORT", "8080")

# Generated per run, not hard-coded: the leak test greps the tree for this exact value, and a
# literal here would match its own definition rather than a real leak.
POSTGRES_PASSWORD = f"it-{secrets.token_urlsafe(24)}"

# The admin bearer for this run (ADR-0004). Generated, not fixed, for the same reason as the
# password above: the leak test greps the tree for the literal value.
ADMIN_TOKEN = f"it-admin-{secrets.token_urlsafe(24)}"
NODE_TOKENS: dict[str, str] = {}
SECRETS_DIR = Path(tempfile.mkdtemp(prefix="coire-it-secrets-"))
RUN_WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="coire-it-workspaces-"))
# The run image is deliberately non-root. On Linux CI a bind-mounted tempfile keeps
# mkdtemp's owner-only mode, which prevents UID 65532 from reading request.json and produces
# an opaque runner exit code 1 before the harness can contact the gateway.
RUN_WORKSPACE_ROOT.chmod(0o777)
os.environ.setdefault("COIRE_IT_RUN_WORKSPACE_ROOT", str(RUN_WORKSPACE_ROOT))

INTEGRATION_PORT = os.environ.get("COIRE_IT_PORT", "18080")
INTEGRATION_API_PORT = os.environ.get("COIRE_IT_API_PORT", "18081")
API_URL = f"http://127.0.0.1:{INTEGRATION_PORT}"
DIRECT_API_URL = f"http://127.0.0.1:{INTEGRATION_API_PORT}"

# The integration overlay adds two node agents on a simulated mesh (research R9). Feature 000's
# suite ran against compose.yaml alone; feature 001 needs nodes to exist at all.
OVERRIDE = COMPOSE_DIR / "compose.override.it.yaml"

INTEGRATION_SECRETS = {
    "COIRE_TAG": os.environ.get("COIRE_TAG", "ci"),
    "COIRE_SECRET_POSTGRES_PASSWORD": POSTGRES_PASSWORD,
    "COIRE_SECRET_KEY_SIGNING_SECRET": f"it-{secrets.token_urlsafe(32)}",
    "COIRE_SECRET_ADMIN_TOKEN": ADMIN_TOKEN,
    "COIRE_SECRET_BOOTSTRAP_ADMIN_EMAIL": "admin@integration.test",
    # Nodes are deliberately started without usable credentials. The fixture declares them
    # through the admin API, installs the returned one-time tokens, and recreates the affected
    # services. This proves a fresh database cannot materialise workers by self-registration.
    "COIRE_SECRET_NODE_TOKENS": "{}",
    "COIRE_IT_NODE_TOKEN_A": "unissued-a",
    "COIRE_IT_NODE_TOKEN_B": "unissued-b",
    "COIRE_SECRETS_DIR": str(SECRETS_DIR),
    "COIRE_IT_PORT": INTEGRATION_PORT,
    "COIRE_IT_API_PORT": INTEGRATION_API_PORT,
    "COIRE_CLUSTER_CONFIG_DIR": str(REPO / "tests/integration/testdata"),
    "COIRE_IT_RUN_WORKSPACE_ROOT": str(RUN_WORKSPACE_ROOT),
    # The composed suite can spend several minutes between operations while engines and
    # acquisition workflows settle. Production remains intentionally strict at 30 seconds;
    # the fixture uses the configured upper bound so manually registered test sessions do not
    # expire between assertions (real ops containers heartbeat continuously).
    "OPS_SESSION_STALE_S": "300",
}

ACCESS_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ACCESS_KID = "coire-integration-access"
ACCESS_AUDIENCE = "coire-integration-audience"
ACCESS_ISSUER = ""


def _b64_int(value: int) -> str:
    import base64

    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class _JwksHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/cdn-cgi/access/certs":
            self.send_error(404)
            return
        numbers = ACCESS_KEY.public_key().public_numbers()
        body = json.dumps(
            {
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": ACCESS_KID,
                        "use": "sig",
                        "alg": "RS256",
                        "n": _b64_int(numbers.n),
                        "e": _b64_int(numbers.e),
                    }
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def access_assertion(*, email: str = "admin@integration.test", **claims: object) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": ACCESS_ISSUER,
        "aud": ACCESS_AUDIENCE,
        "sub": "integration-browser-user",
        "email": email,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 120,
    }
    payload.update(claims)
    return jwt.encode(payload, ACCESS_KEY, algorithm="RS256", headers={"kid": ACCESS_KID})


def integration_env(**extra: str) -> dict[str, str]:
    return {**os.environ, **INTEGRATION_SECRETS, **extra}


def drain_runtime(
    client: httpx.Client,
    headers: dict[str, str],
    *,
    timeout: float = 60,
) -> None:
    """Drain prior runtime state through public APIs for independent scenarios."""
    # A prior scenario may deliberately pin a model reservation. Remove that policy first;
    # otherwise the instance can reach a terminal state while its reservation correctly remains
    # held, contaminating the next scenario's capacity assumptions.
    ledgers = client.get("/api/v1/admin/ledger", headers=headers)
    ledgers.raise_for_status()
    for ledger in ledgers.json():
        for reservation in ledger["reservations"]:
            if reservation["holder_type"] == "model" and reservation["pinned"]:
                response = client.patch(
                    f"/api/v1/admin/ledger/reservations/{reservation['id']}",
                    headers=headers,
                    json={"pinned": False},
                )
                assert response.status_code == 204, response.text

    instance_terminal = {"stopped", "failed"}
    instances = client.get("/api/v1/instances", headers=headers)
    instances.raise_for_status()
    waiting: set[str] = set()
    for instance in instances.json():
        if instance["state"] == "ready":
            response = client.delete(f"/api/v1/instances/{instance['id']}", headers=headers)
            assert response.status_code == 202, response.text
            waiting.add(str(instance["id"]))
        elif instance["state"] not in instance_terminal:
            waiting.add(str(instance["id"]))
    deadline = time.monotonic() + timeout
    while waiting and time.monotonic() < deadline:
        current = client.get("/api/v1/instances", headers=headers)
        current.raise_for_status()
        waiting -= {
            str(instance["id"])
            for instance in current.json()
            if instance["state"] in instance_terminal
        }
        if waiting:
            time.sleep(0.25)
    assert not waiting, f"instances did not drain: {sorted(waiting)}"

    engine_terminal = {"stopped", "failed"}
    engines = client.get("/api/v1/admin/engines", headers=headers)
    engines.raise_for_status()
    active = [engine for engine in engines.json() if engine["state"] not in engine_terminal]
    for engine in active:
        response = client.delete(f"/api/v1/admin/engines/{engine['id']}", headers=headers)
        assert response.status_code == 202, response.text
    active_ids = {str(engine["id"]) for engine in active}
    deadline = time.monotonic() + timeout
    while active_ids and time.monotonic() < deadline:
        current = client.get("/api/v1/admin/engines", headers=headers)
        current.raise_for_status()
        active_ids -= {
            str(engine["id"]) for engine in current.json() if engine["state"] in engine_terminal
        }
        if active_ids:
            time.sleep(0.25)
    assert not active_ids, f"engines did not stop: {sorted(active_ids)}"

    deadline = time.monotonic() + timeout
    remaining_reservations: list[str] = []
    while time.monotonic() < deadline:
        ledgers = client.get("/api/v1/admin/ledger", headers=headers)
        ledgers.raise_for_status()
        remaining_reservations = [
            str(reservation["id"])
            for ledger in ledgers.json()
                for reservation in ledger["reservations"]
                if reservation["holder_type"] == "model" and reservation["released_at"] is None
            ]
        if not remaining_reservations:
            break
        time.sleep(0.25)
    assert not remaining_reservations, (
        f"model reservations did not release: {sorted(remaining_reservations)}"
    )


def _digest_ref(image: str) -> str:
    inspected = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0 or "@sha256:" not in inspected.stdout:
        raise AssertionError(f"integration image has no digest-pinned reference: {image}")
    return inspected.stdout.strip()


def _declare_and_register_nodes(env: dict[str, str]) -> None:
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    declarations = {
        "coire-edge-a": "coire-edge-a.fabric",
        "coire-edge-b": "coire-edge-b.fabric",
    }
    with httpx.Client(base_url=API_URL, timeout=30) as client:
        for name, data_host in declarations.items():
            response = client.post(
                "/api/v1/admin/nodes",
                headers=headers,
                json={
                    "name": name,
                    "control_host": name,
                    "data_host": data_host,
                    "memory_total_bytes": 64 * 1024**3,
                    "disk_total_bytes": 64 * 1024**3,
                },
            )
            assert response.status_code == 201, response.text
            NODE_TOKENS[name] = str(response.json()["token"])

    token_json = json.dumps(NODE_TOKENS)
    INTEGRATION_SECRETS.update(
        {
            "COIRE_SECRET_NODE_TOKENS": token_json,
            "COIRE_IT_NODE_TOKEN_A": NODE_TOKENS["coire-edge-a"],
            "COIRE_IT_NODE_TOKEN_B": NODE_TOKENS["coire-edge-b"],
        }
    )
    (SECRETS_DIR / "node_tokens").write_text(token_json)
    recreate_env = integration_env(COMPOSE_PROJECT_NAME=PROJECT)
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "up",
            "-d",
            "--force-recreate",
            "coire-api",
            "coire-scheduler",
            "node-a",
            "node-b",
        ],
        cwd=COMPOSE_DIR,
        env=recreate_env,
        check=True,
        capture_output=True,
    )
    deadline = time.monotonic() + 60
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        while time.monotonic() < deadline:
            try:
                state = client.get("/api/v1/state", headers=headers).json()
                admin_nodes = client.get("/api/v1/admin/nodes", headers=headers).json()
                ready = {
                    item["name"]
                    for item in state["nodes"]
                    if item["reachability"] == "healthy" and item["budget_bytes"] > 0
                }
                reporting_capacity = {
                    item["name"]
                    for item in admin_nodes
                    if item["status"] is not None
                    and item["status"].get("memory_budget_bytes", 0) > 0
                }
                if ready >= set(declarations) and reporting_capacity >= set(declarations):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(1)
    raise AssertionError("declared integration nodes did not register")


@pytest.fixture(scope="session", autouse=True)
def stack() -> Iterator[None]:
    """Bring the control plane up for the whole session and tear it down at the end."""
    if os.environ.get("COIRE_INTEGRATION") != "1":
        yield
        return

    INTEGRATION_SECRETS.update(
        {
            "COIRE_IT_RUN_AGENT_IMAGE": _digest_ref("coire-agent:ci"),
            "COIRE_IT_RUN_RELAY_IMAGE": _digest_ref("coire-run-relay:ci"),
        }
    )

    global ACCESS_ISSUER
    jwks_server = ThreadingHTTPServer(("0.0.0.0", 0), _JwksHandler)
    jwks_thread = threading.Thread(target=jwks_server.serve_forever, daemon=True)
    jwks_thread.start()
    ACCESS_ISSUER = f"http://host.docker.internal:{jwks_server.server_port}"
    INTEGRATION_SECRETS.update(
        {
            "CLOUDFLARE_ACCESS_ISSUER": ACCESS_ISSUER,
            "CLOUDFLARE_ACCESS_AUDIENCE": ACCESS_AUDIENCE,
        }
    )
    env = integration_env(COMPOSE_PROJECT_NAME=PROJECT)
    # Start from nothing: a leftover volume carries the previous run's credentials.
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "-v", "--remove-orphans"],
        cwd=COMPOSE_DIR,
        env=env,
        capture_output=True,
    )
    up = subprocess.run(
        [str(UP), "--secrets-from-env", "--no-build"],
        env=env,
        capture_output=True,
        text=True,
    )
    if up.returncode != 0:
        # Dump everything needed to diagnose without a second CI run.
        logs = subprocess.run(
            ["docker", "compose", "-p", PROJECT, "logs", "--no-color", "--tail=60"],
            cwd=COMPOSE_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        ps = subprocess.run(
            ["docker", "compose", "-p", PROJECT, "ps", "-a"],
            cwd=COMPOSE_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        pytest.fail(
            "coire-up failed (exit "
            f"{up.returncode})\n--- stdout ---\n{up.stdout}\n--- stderr ---\n{up.stderr}"
            f"\n--- ps ---\n{ps.stdout}\n--- logs ---\n{logs.stdout}",
            pytrace=False,
        )
    _declare_and_register_nodes(env)
    try:
        yield
    finally:
        jwks_server.shutdown()
        jwks_server.server_close()
        jwks_thread.join(timeout=5)
        if os.environ.get("COIRE_IT_KEEP_STACK") != "1":
            subprocess.run(
                ["docker", "compose", "-p", PROJECT, "down", "-v", "--remove-orphans"],
                cwd=COMPOSE_DIR,
                env=env,
                capture_output=True,
            )
            shutil.rmtree(SECRETS_DIR, ignore_errors=True)
            shutil.rmtree(RUN_WORKSPACE_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def api_url() -> str:
    return API_URL


@pytest.fixture(scope="session")
def direct_api_url() -> str:
    """Test-only loopback route that exposes ASGI disconnects without nginx mediation."""
    return DIRECT_API_URL


@pytest.fixture(scope="session")
def admin_headers() -> dict[str, str]:
    """The ADR-0004 admin bearer for this run."""
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(scope="session")
def access_token_factory():  # type: ignore[no-untyped-def]
    return access_assertion


@pytest.fixture(scope="session")
def node_tokens() -> dict[str, str]:
    return dict(NODE_TOKENS)
