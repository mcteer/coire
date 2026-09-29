"""Typed and authenticated workspace preparation on the Studio control listener."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coire_core.models.harness import HarnessRunRequest, ProfileName, TaskClass
from coire_core.models.node import (
    WorkspaceCleanupRequest,
    WorkspacePrepareRequest,
    WorkspacePrepareResult,
)
from coire_core.models.registry import CapabilityProfile
from coire_core.settings import Settings
from coire_node.testing.harness import TOKEN, Agent
from coire_node.workspaces import WorkspaceError, WorkspaceManager


def _command(
    run_id: uuid.UUID, *, source_url: str = "https://github.com/owner/repo.git"
) -> dict[str, Any]:
    request = HarnessRunRequest(
        profile=ProfileName.CODING,
        variant_id=uuid.uuid4(),
        task_class=TaskClass.READ,
        task="find the entrypoint",
        capability_profile=CapabilityProfile(),
        context_window=2048,
    )
    return WorkspacePrepareRequest.model_validate(
        {
            "run_id": str(run_id),
            "source": {"repository_url": source_url, "revision": "main"},
            "task_class": "read",
            "harness_request": request.model_dump(mode="json"),
        }
    ).model_dump(mode="json")


class StubWorkspaces:
    def __init__(self) -> None:
        self.prepared: list[uuid.UUID] = []
        self.cleaned: list[uuid.UUID] = []

    async def prepare(self, command: WorkspacePrepareRequest) -> WorkspacePrepareResult:
        self.prepared.append(command.run_id)
        return WorkspacePrepareResult(
            run_id=command.run_id,
            workspace_ref=f"mcp-{command.run_id.hex}",
            output_ref=f"mcp-out-{command.run_id.hex}",
            source_revision="a" * 40,
            prepared_at=datetime.now(UTC),
        )

    async def cleanup(self, command: WorkspaceCleanupRequest) -> None:
        self.cleaned.append(command.run_id)


class StubDocker:
    async def inspect_container(self, name: str) -> None:
        return None


class StubRuns:
    def __init__(self) -> None:
        self.workspaces = StubWorkspaces()
        self.docker = StubDocker()

    @staticmethod
    def container_name(run_id: uuid.UUID) -> str:
        return f"coire-run-{run_id}"


class LocalWorkspaces(WorkspaceManager):
    """Exercise preparation boundaries without reaching an external Git host."""

    async def _check_public_dns(self, host: str) -> str:
        assert host == "github.com"
        return "140.82.112.3"

    async def _git(
        self,
        args: list[str],
        *,
        cwd: Path,
        max_bytes: int,
        timeout_seconds: int,
        resolved_host: tuple[str, str] | None = None,
    ) -> str:
        if args[0] == "fetch":
            assert resolved_host == ("github.com", "140.82.112.3")
        if args[0] == "checkout":
            (cwd / "README.md").write_text("sample\n", encoding="utf-8")
        return "a" * 40 if args[0] == "rev-parse" else ""


def test_workspace_routes_require_node_auth_and_validate_run_identity(tmp_path: Path) -> None:
    agent = Agent(tmp_path)
    app = agent.app()
    stub = StubRuns()
    app.state.runs = stub
    run_id = uuid.uuid4()
    try:
        with TestClient(app) as anonymous:
            assert anonymous.post("/node/workspaces", json=_command(run_id)).status_code == 401
        with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            response = client.post("/node/workspaces", json=_command(run_id))
            assert response.status_code == 201
            assert response.json()["workspace_ref"] == f"mcp-{run_id.hex}"
            assert (
                client.post(
                    f"/node/workspaces/{uuid.uuid4()}/cleanup",
                    json={"run_id": str(run_id)},
                ).status_code
                == 422
            )
            assert (
                client.post(
                    f"/node/workspaces/{run_id}/cleanup",
                    json={"run_id": str(run_id)},
                ).status_code
                == 204
            )
        assert stub.workspaces.prepared == [run_id]
        assert stub.workspaces.cleaned == [run_id]
    finally:
        agent.close()


def test_source_host_is_allowlisted_before_git_or_dns(tmp_path: Path) -> None:
    manager = WorkspaceManager(
        Settings(
            _secrets_dir="/nonexistent",
            run_workspace_root=str(tmp_path),
            mcp_source_hosts="github.com",
        )  # type: ignore[call-arg]
    )
    allowed = WorkspacePrepareRequest.model_validate(_command(uuid.uuid4()))
    assert manager._source_host(allowed) == "github.com"
    forbidden = WorkspacePrepareRequest.model_validate(
        _command(uuid.uuid4(), source_url="https://127.0.0.1/repo.git")
    )
    with pytest.raises(WorkspaceError, match="not allowed"):
        manager._source_host(forbidden)


@pytest.mark.asyncio
async def test_preparation_is_idempotent_and_cleanup_removes_only_this_run(tmp_path: Path) -> None:
    manager = LocalWorkspaces(
        Settings(_secrets_dir="/nonexistent", run_workspace_root=str(tmp_path))  # type: ignore[call-arg]
    )
    run_id = uuid.uuid4()
    command = WorkspacePrepareRequest.model_validate(_command(run_id))
    first = await manager.prepare(command)
    assert (tmp_path / first.workspace_ref / ".coire" / "request.json").is_file()
    assert (tmp_path / first.workspace_ref / ".coire" / "request.json").stat().st_mode & 0o222 == 0
    assert (tmp_path / first.workspace_ref / "README.md").read_text() == "sample\n"
    assert (tmp_path / first.output_ref).is_dir()
    activity = tmp_path / first.output_ref / f"activity-{run_id}.jsonl"
    activity.write_text('{"safe":"receipt"}\n')
    assert await manager.prepare(command) == first
    changed = command.model_copy(update={"timeout_seconds": command.timeout_seconds + 1})
    with pytest.raises(WorkspaceError, match="another workspace request"):
        await manager.prepare(changed)
    await manager.cleanup(WorkspaceCleanupRequest(run_id=run_id))
    assert not (tmp_path / first.workspace_ref).exists()
    assert not (tmp_path / first.output_ref).exists()
    assert not activity.exists()


async def test_silent_git_process_is_killed_at_prepare_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = asyncio.Event()

    class SilentProcess:
        pid = 424242
        returncode: int | None = None
        stdout = None

        async def wait(self) -> int:
            await stopped.wait()
            self.returncode = -9
            return -9

    async def spawn(*_args: object, **_kwargs: object) -> SilentProcess:
        return SilentProcess()

    monkeypatch.setattr("coire_node.workspaces.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("coire_node.workspaces.os.killpg", lambda _pid, _signal: stopped.set())
    manager = WorkspaceManager(
        Settings(_secrets_dir="/nonexistent", run_workspace_root=str(tmp_path))  # type: ignore[call-arg]
    )
    with pytest.raises(WorkspaceError, match="timed out"):
        await manager._git(["fetch"], cwd=tmp_path, max_bytes=1024, timeout_seconds=0)
    assert stopped.is_set()


async def test_clone_dns_pin_rejects_private_address_in_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Resolver:
        async def getaddrinfo(self, _host: str, _port: int, **_kwargs: object):  # type: ignore[no-untyped-def]
            return [
                (None, None, None, None, ("140.82.112.3", 443)),
                (None, None, None, None, ("127.0.0.1", 443)),
            ]

    monkeypatch.setattr("coire_node.workspaces.asyncio.get_running_loop", lambda: Resolver())
    manager = WorkspaceManager(
        Settings(_secrets_dir="/nonexistent", run_workspace_root=str(tmp_path))  # type: ignore[call-arg]
    )
    with pytest.raises(WorkspaceError, match="outside public space"):
        await manager._check_public_dns("github.com")
