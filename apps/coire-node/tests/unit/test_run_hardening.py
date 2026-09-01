from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from coire_core.models.runs import RunContainerCreate, RunLimits
from coire_core.settings import Settings
from coire_node.runs import RunManager, RunRuntimeError


class NoopDocker:
    pass


def command(workspace: str) -> RunContainerCreate:
    return RunContainerCreate(
        run_id=uuid.uuid4(),
        image=f"ghcr.io/mcteer/coire-agent@sha256:{'a' * 64}",
        argv=["python", "-m", "coire_agent"],
        workspace_ref=workspace,
        run_token="r" * 48,
        gateway_url="http://coire-core.lab:8080/v1",
        limits=RunLimits(),
    )


def test_create_payload_is_hardened_and_has_no_general_egress(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "safe"
    workspace.mkdir(parents=True)
    settings = Settings(_secrets_dir="/none", run_workspace_root=str(workspace.parent))
    manager = RunManager(settings, NoopDocker())  # type: ignore[arg-type]
    payload = manager.create_payload(command("safe"), "coire-run-network")

    host = payload["HostConfig"]
    assert payload["User"] == "65532:65532"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Privileged"] is False
    assert host["Memory"] == host["MemorySwap"] == 4 * 1024**3
    assert host["PidsLimit"] == 256
    assert host["PortBindings"] == {} and host["PublishAllPorts"] is False
    assert host["NetworkMode"] == "coire-run-network"
    assert host["RestartPolicy"]["Name"] == "no"
    assert all("docker.sock" not in mount for mount in host["Binds"])


def test_workspace_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    manager = RunManager(
        Settings(_secrets_dir="/none", run_workspace_root=str(root)),
        NoopDocker(),  # type: ignore[arg-type]
    )
    with pytest.raises(RunRuntimeError, match="escapes"):
        manager.workspace("link")


def test_docker_multiplexed_logs_are_decoded() -> None:
    payload = b"hello\n"
    frame = b"\x01\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload
    assert RunManager._decode_docker_logs(frame) == "hello\n"
