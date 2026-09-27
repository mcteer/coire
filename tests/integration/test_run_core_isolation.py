"""Static production topology proofs for Studio-only user runs."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMPOSE = (REPO / "deploy/compose/compose.yaml").read_text()


def service_section(name: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", COMPOSE)
    assert match is not None
    return match.group(1)


def test_core_compose_has_no_user_run_service_or_agent_image() -> None:
    assert "dockerfile: apps/coire-agent/Dockerfile" not in COMPOSE
    assert not re.search(r"^\s+coire-agent:\s*$", COMPOSE, re.MULTILINE)
    assert not re.search(r"^\s+coire-run-relay:\s*$", COMPOSE, re.MULTILINE)


def test_api_and_scheduler_have_no_raw_or_remote_studio_docker_socket() -> None:
    assert "/var/run/docker.sock:/var/run/docker.sock" in COMPOSE  # proxy only
    for service in ("coire-api", "coire-scheduler"):
        section = service_section(service)
        assert "/var/run/docker.sock" not in section
        assert "ssh://" not in section
    scheduler = service_section("coire-scheduler")
    assert "DOCKER_HOST: tcp://docker-socket-proxy:2375" in scheduler
