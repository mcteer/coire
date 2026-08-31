"""Topology invariants (T035).

Asserts every invariant in `specs/000-bootstrap/contracts/compose-topology.md` against the
rendered compose configuration. Needs no running stack, so it guards the contract on every
pull request rather than only during a bring-up.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]

# Needs the docker CLI to render the compose config, but not a running stack — so it is not
# marked `integration` and runs on every pull request. Without Docker it skips rather than
# producing 27 collection errors.
pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="requires the docker CLI to render compose config"
)
COMPOSE = REPO / "deploy/compose/compose.yaml"

FIRST_PARTY = {"coire-web", "coire-api", "coire-mcp", "coire-scheduler", "coire-migrate"}
LONG_LIVED = {
    "coire-web",
    "coire-api",
    "coire-mcp",
    "coire-scheduler",
    "postgres",
    "docker-socket-proxy",
    "otel-collector",
}
EXPECTED_NETWORKS = {
    "coire-edge",
    "coire-db",
    "coire-internal",
    "coire-docker",
    "coire-telemetry",
    "coire-node-ingress",
}


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    env = {
        **os.environ,
        "COIRE_SECRETS_DIR": "/tmp/coire-secrets-test",
        "COIRE_REGISTRY": "",
        "COIRE_TAG": "dev",
        "COIRE_CONTROL_BIND_ADDRESS": "127.0.0.1",
        "COIRE_CONTROL_PORT": "8180",
        # conftest sets COMPOSE_FILE so `coire-up` picks up the integration overlay. These
        # invariants are about what *ships*, so render the production file alone: an overlay
        # leaking in here would quietly weaken every assertion below.
        "COMPOSE_FILE": str(COMPOSE),
    }
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        capture_output=True,
        text=True,
        env=env,
        cwd=COMPOSE.parent,
    )
    if proc.returncode != 0:
        pytest.fail(f"docker compose config failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def nets(config: dict[str, Any], service: str) -> set[str]:
    return set((config["services"][service].get("networks") or {}).keys())


class TestNetworkSegmentation:
    def test_exactly_six_networks(self, config: dict[str, Any]) -> None:
        assert set(config["networks"]) == EXPECTED_NETWORKS

    def test_only_host_ingress_networks_are_external(self, config: dict[str, Any]) -> None:
        for name, net in config["networks"].items():
            expected = name not in {"coire-edge", "coire-node-ingress"}
            assert bool(net.get("internal", False)) is expected, name

    def test_node_ingress_is_collector_only(self, config: dict[str, Any]) -> None:
        attached = {
            name
            for name, service in config["services"].items()
            if "coire-node-ingress" in (service.get("networks") or {})
        }
        assert attached == {"otel-collector"}

    def test_web_cannot_reach_postgres(self, config: dict[str, Any]) -> None:
        """The headline segmentation invariant (FR-006)."""
        assert "coire-db" not in nets(config, "coire-web")
        assert nets(config, "coire-web") & nets(config, "postgres") == set()

    def test_socket_proxy_reachable_only_by_scheduler(self, config: dict[str, Any]) -> None:
        """FR-007: the Docker socket has exactly two parties on its network."""
        on_docker = {s for s in config["services"] if "coire-docker" in nets(config, s)}
        assert on_docker == {"coire-scheduler", "docker-socket-proxy"}

    def test_postgres_is_only_on_the_db_network(self, config: dict[str, Any]) -> None:
        assert nets(config, "postgres") == {"coire-db"}


class TestPublishedPorts:
    def test_only_control_ingress_and_otlp_publish(self, config: dict[str, Any]) -> None:
        """Feature 022 adds node OTLP; no database or internal service is published."""
        for name, svc in config["services"].items():
            ports = svc.get("ports") or []
            if name not in {"coire-web", "otel-collector"}:
                assert not ports, f"{name} publishes {ports}"
            else:
                assert len(ports) == 1
                assert ports[0].get("host_ip") in ("127.0.0.1", "::1")
        assert config["services"]["coire-web"]["ports"][0]["target"] == 8080
        assert config["services"]["otel-collector"]["ports"][0]["target"] == 4317


class TestHardening:
    @pytest.mark.parametrize("service", sorted(FIRST_PARTY))
    def test_first_party_hardening(self, config: dict[str, Any], service: str) -> None:
        svc = config["services"][service]
        assert svc.get("read_only") is True, f"{service} rootfs is writable"
        assert "/tmp" in (svc.get("tmpfs") or []), f"{service} has no tmpfs /tmp"
        assert svc.get("cap_drop") == ["ALL"], f"{service} does not drop all capabilities"
        assert "no-new-privileges:true" in (svc.get("security_opt") or [])
        user = str(svc.get("user") or "")
        assert user and not user.startswith("0"), f"{service} runs as {user or 'root'}"

    def test_postgres_is_hardened_as_far_as_upstream_allows(self, config: dict[str, Any]) -> None:
        """Its shell is accepted (research R3) but it is still confined."""
        svc = config["services"]["postgres"]
        assert svc.get("read_only") is True
        assert "no-new-privileges:true" in (svc.get("security_opt") or [])
        assert svc.get("cap_drop") == ["ALL"]


class TestHealthAndDependencies:
    @pytest.mark.parametrize("service", sorted(LONG_LIVED))
    def test_every_long_lived_service_has_a_healthcheck(
        self, config: dict[str, Any], service: str
    ) -> None:
        if service == "docker-socket-proxy":
            pytest.skip("third-party image ships its own supervision; no healthcheck exposed")
        assert config["services"][service].get("healthcheck"), service

    def test_web_healthcheck_is_local_not_proxied(self, config: dict[str, Any]) -> None:
        """Probing a proxied path would make an api restart mark web unhealthy (SC-002)."""
        test = " ".join(config["services"]["coire-web"]["healthcheck"]["test"])
        assert "/nginx-health" in test
        assert "/ready" not in test

    def test_api_waits_for_postgres_and_migrations(self, config: dict[str, Any]) -> None:
        dep = config["services"]["coire-api"]["depends_on"]
        assert dep["postgres"]["condition"] == "service_healthy"
        assert dep["coire-migrate"]["condition"] == "service_completed_successfully"

    def test_nothing_depends_on_the_collector(self, config: dict[str, Any]) -> None:
        """Telemetry loss must never cause a request failure (FR-014, research R11)."""
        for name, svc in config["services"].items():
            assert "otel-collector" not in (svc.get("depends_on") or {}), name


class TestImagesAndSecrets:
    def test_third_party_images_are_digest_pinned(self, config: dict[str, Any]) -> None:
        for name in ("postgres", "docker-socket-proxy"):
            image = config["services"][name]["image"]
            assert "@sha256:" in image, f"{name} is not digest-pinned: {image}"

    def test_first_party_images_are_tag_controlled(self, config: dict[str, Any]) -> None:
        """First-party tags come from COIRE_TAG; CI pins them to released digests."""
        for name in FIRST_PARTY | {"otel-collector"}:
            assert config["services"][name]["image"], name

    def test_no_service_uses_the_agent_image(self, config: dict[str, Any]) -> None:
        """FR-017: coire-agent is built by core but never run on it."""
        for name, svc in config["services"].items():
            assert "coire-agent" not in svc.get("image", ""), f"{name} runs the agent image"

    def test_the_ci_only_node_image_is_absent_from_production(self, config: dict[str, Any]) -> None:
        """`coire-node-test` carries a shell so the restart test can kill the agent. It is
        exempt from the image policy precisely because it never ships; this is the assertion
        that makes that exemption safe."""
        for name, svc in config["services"].items():
            assert "node-test" not in svc.get("image", ""), f"{name} uses the CI-only image"
        assert "node-a" not in config["services"]
        assert "node-b" not in config["services"]

    def test_no_service_carries_a_hugging_face_credential(self, config: dict[str, Any]) -> None:
        """Spec FR-005: the Hugging Face token exists only on a node agent's own machine.

        Checked against the *production* project, which is what ships. The integration overlay
        gives node-a an HF_TOKEN deliberately, and that overlay is never rendered here.
        """
        for name, svc in config["services"].items():
            for key in svc.get("environment") or {}:
                assert "HF" not in key.upper().replace("SHF", ""), f"{name} has {key}"
            for secret in svc.get("secrets") or []:
                assert "hf" not in str(secret.get("source", "")).lower(), name

    def test_secrets_are_file_sourced(self, config: dict[str, Any]) -> None:
        """Compose rejects `environment:` sources for read_only services (research R4)."""
        for name, secret in config["secrets"].items():
            assert "file" in secret, f"secret {name} is not file-sourced"

    def test_secrets_live_outside_the_repository(self, config: dict[str, Any]) -> None:
        for name, secret in config["secrets"].items():
            assert not str(secret["file"]).startswith(str(REPO)), f"{name} is inside the repo"
