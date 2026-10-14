"""Hosted native endpoint bridges cannot select operator transport authority."""

import json
from pathlib import Path

import pytest
from integration.upgrade_native_ci_transport import forward_arguments, scoped_ssh


def test_forwards_name_only_private_sockets_and_loopback_endpoints() -> None:
    arguments = forward_arguments((39260, 39262), (39261, 39263), 64229)
    assert arguments == [
        "-R",
        "/tmp/coire019-ci/bridges/node-a-control.sock:127.0.0.1:39260",
        "-R",
        "/tmp/coire019-ci/bridges/node-a-data.sock:127.0.0.1:39261",
        "-R",
        "/tmp/coire019-ci/bridges/node-b-control.sock:127.0.0.1:39262",
        "-R",
        "/tmp/coire019-ci/bridges/node-b-data.sock:127.0.0.2:39263",
        "-L",
        "127.0.0.1:64229:127.0.0.1:64229",
    ]


def test_native_pair_can_share_data_port_on_distinct_loopback_addresses() -> None:
    arguments = forward_arguments((39260, 39262), (39261, 39261), 64229)
    assert "/tmp/coire019-ci/bridges/node-a-data.sock:127.0.0.1:39261" in arguments
    assert "/tmp/coire019-ci/bridges/node-b-data.sock:127.0.0.2:39261" in arguments


@pytest.mark.parametrize("port", [True, 80, 65536, 39260])
def test_invalid_or_overlapping_endpoints_are_refused(port: int) -> None:
    with pytest.raises(ValueError):
        forward_arguments((39260, 39262), (39261, 39263), port)


def test_transport_binds_hosted_vm_metadata_and_disables_control_connection_reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "integration.upgrade_native_ci_transport.require_hosted_mac", lambda root: None
    )
    root = tmp_path / "vm"
    root.mkdir(mode=0o700)
    config = root / "instances/coire019-native-ci/ssh.config"
    config.parent.mkdir(parents=True)
    config.write_text("Host lima-coire019-native-ci\n")
    config.chmod(0o600)
    metadata = root / "vm.json"
    body = {
        "execution": "github-hosted-native-ci",
        "host_mounts": [],
        "acceleration": "tcg",
        "ssh_config": str(config),
    }
    metadata.write_text(json.dumps(body))
    metadata.chmod(0o600)
    command = scoped_ssh(root)
    assert command[:3] == ["ssh", "-F", str(config)]
    assert "-oControlPath=none" in command
    assert command[-2:] == ["-l", "coire019-ci"]
    body["host_mounts"] = ["/Users/operator"]
    metadata.write_text(json.dumps(body))
    with pytest.raises(RuntimeError, match="isolated VM"):
        scoped_ssh(root)
