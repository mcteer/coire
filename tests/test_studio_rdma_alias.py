"""Alias-only plans and verified rollback without touching a real network."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def alias_helper(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = Path(__file__).parents[1] / "deploy/cluster/scripts/studio-rdma-alias.py"
    spec = importlib.util.spec_from_file_location("studio_rdma_alias", path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper.socket, "gethostbyname", lambda host: "192.168.100.11")
    return helper


class Fabric:
    def __init__(self, alias: str, *, fault: str | None = None) -> None:
        self.alias = alias
        self.configured = False
        self.fault = fault
        self.calls: list[list[str]] = []

    def is_configured(self) -> bool:
        return self.configured

    def run(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if argv == ["/sbin/ifconfig", "bridge0"]:
            return "bridge0: flags=8843<UP,BROADCAST,RUNNING>\n inet 192.168.100.11 netmask 0xffffff00\n member: en5 flags=3\n"
        if argv == ["/sbin/ifconfig", "en5"]:
            ipv4 = f" inet {self.alias} netmask 0xfffffffc\n" if self.configured else ""
            return "en5: flags=8843<UP,BROADCAST,RUNNING>\n status: active\n" + ipv4
        if argv[:4] == ["/sbin/route", "-n", "get", "coire-core.lab"]:
            return " interface: en1\n gateway: 192.0.2.1\n"
        if argv[:3] == ["/sbin/route", "-n", "get"]:
            return " interface: bridge0\n gateway: link#10\n"
        if argv == ["/usr/bin/rdma_ctl", "status"]:
            return "enabled"
        if argv == ["/usr/bin/ibv_devinfo", "-d", "rdma_en5"]:
            return "PORT_ACTIVE"
        if argv == ["/usr/bin/ibv_devinfo", "-v", "-d", "rdma_en5"]:
            return "GID[0]: fe80::1234" if self.fault == "gid" else f"GID[1]: ::ffff:{self.alias}"
        if argv == [
            "/sbin/ifconfig",
            "en5",
            "inet",
            self.alias,
            "netmask",
            "255.255.255.252",
            "alias",
        ]:
            if self.fault == "add":
                raise RuntimeError("synthetic kernel refusal")
            self.configured = True
            return ""
        if argv == ["/sbin/ifconfig", "en5", "inet", self.alias, "-alias"]:
            if self.fault == "remove":
                raise RuntimeError("synthetic removal refusal")
            self.configured = False
            return ""
        raise AssertionError(argv)


def test_alias_apply_preserves_bridge_and_root_baseline_then_rolls_back(
    alias_helper: Any,
    tmp_path: Path,
) -> None:
    fabric = Fabric(alias_helper.alias_for("coire-edge-a"))
    path = tmp_path / "private" / "baseline.json"
    alias_helper.apply("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    saved = path.read_bytes()
    assert fabric.is_configured() and not path.stat().st_mode & 0o077
    assert sum("-v" in argv for argv in fabric.calls) == 5
    alias_helper.rollback(path, fabric.run)
    assert not fabric.is_configured() and path.read_bytes() == saved
    assert all(
        "addm" not in argv and "deletem" not in argv and "down" not in argv for argv in fabric.calls
    )
    assert all(argv[0] != "/sbin/route" or argv[1:3] == ["-n", "get"] for argv in fabric.calls)


@pytest.mark.parametrize("fault", ["add", "gid"])
def test_alias_failure_restores_original_interface_without_bridge_operations(
    alias_helper: Any,
    tmp_path: Path,
    fault: str,
) -> None:
    fabric = Fabric(alias_helper.alias_for("coire-edge-a"), fault=fault)
    with pytest.raises((ValueError, RuntimeError)):
        alias_helper.apply(
            "coire-edge-a",
            "en5",
            tmp_path / "private" / "baseline.json",
            fabric.run,
            lambda _: None,
        )
    assert not fabric.configured


def test_saved_baseline_and_unexpected_alias_prevent_reapplication(
    alias_helper: Any,
    tmp_path: Path,
) -> None:
    fabric = Fabric(alias_helper.alias_for("coire-edge-a"))
    path = tmp_path / "private" / "baseline.json"
    alias_helper.apply("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    alias_helper.rollback(path, fabric.run)
    with pytest.raises(ValueError, match="baseline retained"):
        alias_helper.apply("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    fabric.configured = True
    with pytest.raises(ValueError, match="already has"):
        alias_helper.apply("coire-edge-a", "en5", path, fabric.run, lambda _: None)


def test_removed_gid_and_failed_rollback_never_report_success(
    alias_helper: Any,
    tmp_path: Path,
) -> None:
    fabric = Fabric(alias_helper.alias_for("coire-edge-a"))
    path = tmp_path / "private" / "baseline.json"
    alias_helper.apply("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    fabric.fault = "remove"
    with pytest.raises(RuntimeError, match="removal refusal"):
        alias_helper.rollback(path, fabric.run)
    assert fabric.configured


def test_generated_aliases_are_distinct_inside_one_link_local_network(alias_helper: Any) -> None:
    import ipaddress

    aliases = [ipaddress.IPv4Address(alias_helper.alias_for(node)) for node in alias_helper.NODES]
    assert aliases[0] != aliases[1] and all(ip.is_link_local for ip in aliases)
    network = ipaddress.IPv4Network(f"{aliases[0]}/30", strict=False)
    assert aliases[1] in network


def test_restore_after_reboot_reuses_baseline_and_is_idempotent(
    alias_helper: Any, tmp_path: Path
) -> None:
    fabric = Fabric(alias_helper.alias_for("coire-edge-a"))
    path = tmp_path / "private/baseline.json"
    alias_helper.apply("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    saved = path.read_bytes()
    fabric.configured = False  # A cold boot loses only the runtime alias.
    fabric.calls.clear()
    alias_helper.restore("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    assert fabric.configured and path.read_bytes() == saved
    changes = [argv for argv in fabric.calls if "alias" in argv]
    assert len(changes) == 1
    fabric.calls.clear()
    alias_helper.restore("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    assert not any("alias" in argv or "-alias" in argv for argv in fabric.calls)
    assert path.read_bytes() == saved


def test_restore_failed_gid_removes_only_reintroduced_alias(
    alias_helper: Any, tmp_path: Path
) -> None:
    fabric = Fabric(alias_helper.alias_for("coire-edge-a"))
    path = tmp_path / "private/baseline.json"
    alias_helper.apply("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    saved = path.read_bytes()
    fabric.configured = False
    fabric.fault = "gid"
    fabric.calls.clear()
    with pytest.raises(ValueError, match="mapped GID"):
        alias_helper.restore("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    assert not fabric.configured and path.read_bytes() == saved
    assert all(
        "addm" not in argv and "deletem" not in argv and "down" not in argv for argv in fabric.calls
    )
    assert all(argv[0] != "/sbin/route" or argv[1:3] == ["-n", "get"] for argv in fabric.calls)


def test_restore_refuses_wrong_baseline_scope_before_mutation(
    alias_helper: Any, tmp_path: Path
) -> None:
    fabric = Fabric(alias_helper.alias_for("coire-edge-a"))
    path = tmp_path / "private/baseline.json"
    alias_helper.apply("coire-edge-a", "en5", path, fabric.run, lambda _: None)
    fabric.configured = False
    fabric.calls.clear()
    with pytest.raises(ValueError, match="identity"):
        alias_helper.restore("coire-edge-b", "en5", path, fabric.run, lambda _: None)
    assert fabric.calls == []
