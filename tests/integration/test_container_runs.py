"""Real local-Docker proof of the hardened run/relay lifecycle."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from coire_core.models.harness import ProfileName
from coire_core.models.runs import RunContainerCreate, RunLimits, RunReconcileRequest
from coire_core.settings import Settings
from coire_node.docker_api import DockerAPI
from coire_node.run_reconciler import RunReconciler
from coire_node.runs import RunManager

pytestmark = pytest.mark.integration


def image_ref(name: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", name, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode or "@sha256:" not in result.stdout:
        pytest.skip(f"required local image is absent: {name}")
    return result.stdout.strip()


def docker_available() -> bool:
    return Path("/var/run/docker.sock").exists()


def prepare_workspace(root: Path, variant_id: uuid.UUID) -> Path:
    workspace = root / "workspaces" / "workspace-1" / ".coire"
    workspace.mkdir(parents=True)
    (workspace / "request.json").write_text(
        json.dumps(
            {
                "profile": "general",
                "variant_id": str(variant_id),
                "task_class": "read",
                "task": "answer with JSON",
                "capability_profile": {"structured_output": "json_mode"},
                "context_window": 4096,
                "thinking_token_limit": 32,
            }
        )
    )
    return workspace


class Gateway(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        size = int(self.headers.get("content-length", "0"))
        self.rfile.read(size)
        payload = json.dumps(
            {
                "choices": [{"message": {"content": '{"answer":"ok"}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class RevocableGateway(Gateway):
    entered = threading.Event()
    release = threading.Event()
    revoked_tokens: ClassVar[set[str]] = set()

    @classmethod
    def reset(cls) -> None:
        cls.entered.clear()
        cls.release.clear()
        cls.revoked_tokens.clear()

    def do_POST(self) -> None:
        token = self.headers.get("authorization", "").removeprefix("Bearer ")
        if token in self.revoked_tokens:
            self.send_response(401)
            self.end_headers()
            return
        self.entered.set()
        self.release.wait(timeout=10)
        super().do_POST()


@pytest.mark.anyio
async def test_container_run_uses_only_internal_relay_and_cleans_up(tmp_path: Path) -> None:
    if not docker_available():
        pytest.skip("local Docker socket is unavailable")
    port_socket = socket.socket()
    port_socket.bind(("0.0.0.0", 0))
    port = int(port_socket.getsockname()[1])
    port_socket.close()
    server = ThreadingHTTPServer(("0.0.0.0", port), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    run_id, model_id, variant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace = prepare_workspace(tmp_path, variant_id)
    settings = Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        run_workspace_root=str(workspace.parents[1]),
        run_agent_image=image_ref("coire-agent:ci"),
        run_relay_image=image_ref("coire-run-relay:ci"),
    )
    docker = DockerAPI("/var/run/docker.sock")
    manager = RunManager(settings, docker)
    command = RunContainerCreate(
        run_id=run_id,
        profile=ProfileName.GENERAL,
        model_id=model_id,
        variant_id=variant_id,
        image=settings.run_agent_image,
        argv=["-m", "coire_agent"],
        workspace_ref="workspace-1",
        run_token="r" * 48,
        gateway_url=f"http://host.docker.internal:{port}/v1",
        limits=RunLimits(memory_bytes=256 * 1024**2, timeout_seconds=30),
    )
    try:
        created = await manager.create(command)
        assert created.hardened
        await manager.start(run_id)
        replayed = await RunManager(settings, docker).create(command)
        assert replayed.container_id == created.container_id
        exited = await manager.wait(run_id)
        logs = await manager.logs(run_id)
        assert exited.exit_code == 0, logs[0].content
        assert (await manager.collect(run_id)).result["output"]["answer"] == "ok"
        network = await docker.inspect_network(manager.network_name(run_id))
        assert network is not None and network["Internal"] is True
        attached_names = {item["Name"] for item in (network.get("Containers") or {}).values()}
        assert attached_names <= {
            manager.container_name(run_id),
            manager.relay_name(run_id),
        }
        runner = await docker.inspect_container(manager.container_name(run_id))
        relay = await docker.inspect_container(manager.relay_name(run_id))
        assert runner is not None and relay is not None
        assert manager.network_name(run_id) in runner["NetworkSettings"]["Networks"]
        assert runner["HostConfig"]["ReadonlyRootfs"] is True
        assert runner["HostConfig"]["PortBindings"] == {}
        assert relay["HostConfig"]["PortBindings"] == {}

        reconciled = await RunReconciler(manager).reconcile(
            RunReconcileRequest(authoritative_run_ids=frozenset())
        )
        assert reconciled.orphan_run_ids == [run_id]
        assert reconciled.reaped_run_ids == [run_id]
        assert await docker.inspect_container(manager.container_name(run_id)) is None
    finally:
        await manager.remove(run_id, kill=True)
        assert await docker.inspect_container(manager.container_name(run_id)) is None
        assert await docker.inspect_network(manager.network_name(run_id)) is None
        await docker.close()
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.anyio
async def test_live_run_token_revocation_precedes_sub_five_second_kill(tmp_path: Path) -> None:
    if not docker_available():
        pytest.skip("local Docker socket is unavailable")
    port_socket = socket.socket()
    port_socket.bind(("0.0.0.0", 0))
    port = int(port_socket.getsockname()[1])
    port_socket.close()
    RevocableGateway.reset()
    server = ThreadingHTTPServer(("0.0.0.0", port), RevocableGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    run_id, model_id, variant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace = prepare_workspace(tmp_path, variant_id)
    token = "k" * 48
    settings = Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        run_workspace_root=str(workspace.parents[1]),
        run_agent_image=image_ref("coire-agent:ci"),
        run_relay_image=image_ref("coire-run-relay:ci"),
    )
    docker = DockerAPI("/var/run/docker.sock")
    manager = RunManager(settings, docker)
    command = RunContainerCreate(
        run_id=run_id,
        profile=ProfileName.GENERAL,
        model_id=model_id,
        variant_id=variant_id,
        image=settings.run_agent_image,
        argv=["-m", "coire_agent"],
        workspace_ref="workspace-1",
        run_token=token,
        gateway_url=f"http://host.docker.internal:{port}/v1",
        limits=RunLimits(memory_bytes=256 * 1024**2, timeout_seconds=30),
    )
    try:
        await manager.create(command)
        await manager.start(run_id)
        assert await asyncio.to_thread(RevocableGateway.entered.wait, 5)

        RevocableGateway.revoked_tokens.add(token)
        denied = Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=b"{}",
            headers={"authorization": f"Bearer {token}"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc_info:
            await asyncio.to_thread(urlopen, denied, timeout=2)
        assert exc_info.value.code == 401

        started = time.monotonic()
        await manager.remove(run_id, kill=True)
        assert time.monotonic() - started < 5.0
        assert await docker.inspect_container(manager.container_name(run_id)) is None
    finally:
        RevocableGateway.release.set()
        await manager.remove(run_id, kill=True)
        await docker.close()
        server.shutdown()
        thread.join(timeout=2)
