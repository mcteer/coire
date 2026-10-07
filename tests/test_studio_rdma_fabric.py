"""Endpoint-preserving command plans and partial-cutover recovery without live networking."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def helper() -> ModuleType:
    path = Path(__file__).parents[1] / "deploy/cluster/scripts/studio-rdma-fabric.py"
    spec = importlib.util.spec_from_file_location("studio_rdma_fabric", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.__dict__["read_network_preferences"] = lambda: {
        "VirtualNetworkInterfaces": {
            "Bridge": {"bridge0": {"Interfaces": ["en2", "en3", "en4", "en5", "en6", "en7"]}}
        },
        "NetworkServices": {
            "original-service": {
                "UserDefinedName": "Thunderbolt Bridge",
                "Interface": {"DeviceName": "bridge0"},
                "IPv4": {
                    "ConfigMethod": "Manual",
                    "Addresses": ["192.168.100.11"],
                    "SubnetMasks": ["255.255.255.0"],
                },
            }
        },
    }
    return module


def test_plan_preserves_endpoints_and_never_changes_control_or_firewall(helper: Any) -> None:
    baseline = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    plan = helper.commands(baseline, rollback=False)
    assert plan[-1] == [
        "/sbin/ifconfig",
        "en5",
        "inet",
        "192.168.100.11",
        "netmask",
        "255.255.255.0",
        "up",
    ]
    assert all(argv[0] == "/sbin/ifconfig" for argv in plan)
    assert "en1" not in str(plan) and "192.168.0." not in str(plan)
    rollback = helper.commands(baseline, rollback=True)
    assert not any("addm" in argv for argv in rollback)
    assert rollback[-1] == [
        "/usr/sbin/networksetup",
        "-setnetworkserviceenabled",
        "Thunderbolt Bridge",
        "on",
    ]


@pytest.mark.parametrize("interface", ["en1; reboot", "bridge0", "lo0", "../en5"])
def test_non_native_interface_selectors_are_refused(helper: Any, interface: str) -> None:
    baseline = helper.Snapshot(
        "coire-edge-a", interface, "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    with pytest.raises(ValueError):
        helper.commands(baseline, rollback=False)


def test_baseline_is_private_durable_and_never_overwritten(helper: Any, tmp_path: Path) -> None:
    path = tmp_path / "state" / "baseline.json"
    baseline = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    helper.save_snapshot(path, baseline)
    assert path.stat().st_mode & 0o077 == 0
    assert helper.load_snapshot(path, "coire-edge-a") == baseline
    with pytest.raises(ValueError):
        helper.save_snapshot(path, baseline)
    with pytest.raises(ValueError):
        helper.load_snapshot(path, "coire-edge-b")
    value = json.loads(path.read_text())
    value["address"] = "127.0.0.1"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        helper.load_snapshot(path, "coire-edge-a")


@pytest.mark.parametrize("fail_at", list(range(1, 13)))
def test_every_partial_cutover_restores_bridge_and_endpoint(
    helper: Any, tmp_path: Path, fail_at: int
) -> None:
    baseline = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    state = {
        "member": True,
        "up": True,
        "bridge_address": True,
        "target_address": False,
        "peer_route": False,
        "local_route": False,
    }
    attempts = 0

    def runner(argv: list[str]) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == fail_at:
            raise RuntimeError("injected failure")
        if argv == ["/sbin/ifconfig", "bridge0"]:
            return (
                f"bridge0: flags=<{'UP,' if state['up'] else ''}RUNNING>\n"
                + (" inet 192.168.100.11 netmask 0xffffff00\n" if state["bridge_address"] else "")
                + (" member: en5 flags=3\n" if state["member"] else "")
            )
        if argv == ["/sbin/ifconfig", "en5"]:
            return "en5: flags=<UP,RUNNING>\nstatus: active\nether 36:f9:75:b7:65:cc\n" + (
                " inet 192.168.100.11 netmask 0xffffff00\n" if state["target_address"] else ""
            )
        if argv[0] == "/sbin/route":
            if "get" in argv:
                if argv[-1] == baseline.address:
                    return (
                        "destination: 192.168.100.11\ninterface: lo0\nflags: <UP,HOST,STATIC>\n"
                        if state["local_route"]
                        else "destination: default\ninterface: en1\nflags: <UP,GATEWAY,STATIC>\n"
                    )
                return (
                    "destination: 192.168.100.12\ninterface: en5\nflags: <UP,HOST,STATIC>\n"
                    if state["peer_route"]
                    else "destination: default\ninterface: en1\nflags: <UP,GATEWAY,STATIC>\n"
                )
            if "add" in argv:
                state["local_route" if argv[4] == baseline.address else "peer_route"] = True
            if "delete" in argv:
                state["local_route" if argv[4] == baseline.address else "peer_route"] = False
            return ""
        if argv[0].endswith("arp"):
            return "? (192.168.100.12) at 36:b4:6b:e:32:cc on en5 [ethernet]\n"
        if argv[0].endswith("networksetup"):
            if argv[-1] == "on":
                state["member"] = state["up"] = state["bridge_address"] = True
                state["target_address"] = False
            return ""
        if "down" in argv:
            state["up"] = False
        elif "deletem" in argv:
            state["member"] = False
        elif "addm" in argv:
            state["member"] = True
        elif "-alias" in argv:
            state["bridge_address" if argv[1] == "bridge0" else "target_address"] = False
        elif "inet" in argv:
            state["bridge_address" if argv[1] == "bridge0" else "target_address"] = True
            if argv[1] == "bridge0":
                state["up"] = True
        return ""

    with pytest.raises(RuntimeError):
        helper.apply(baseline, tmp_path / "baseline.json", runner)
    assert state == {
        "member": True,
        "up": True,
        "bridge_address": True,
        "target_address": False,
        "peer_route": False,
        "local_route": False,
    }


def test_network_command_preserves_original_os_stderr(
    helper: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(
            1, ["/sbin/ifconfig"], stderr="ifconfig: actual OS failure"
        )

    monkeypatch.setattr(helper.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        helper.run(["/sbin/ifconfig", "bridge0"])
    assert "actual OS failure" in " ".join(caught.value.__notes__)


def test_managed_recovery_reenables_service_after_disable_failure(helper: Any) -> None:
    snapshot = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    calls = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if argv[-1] == "off":
            raise RuntimeError("authorization refused")
        return ""

    with pytest.raises(RuntimeError, match="authorization"):
        helper.reactivate_saved_bridge(snapshot, runner)
    assert [argv[-1] for argv in calls] == ["off", "on"]


def test_changed_persistent_configuration_refuses_recovery_before_mutation(
    helper: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = helper.read_network_preferences()
    saved["NetworkServices"]["original-service"]["IPv4"]["Addresses"] = ["192.168.100.99"]
    monkeypatch.setattr(helper, "read_network_preferences", lambda: saved)
    calls = []
    snapshot = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return ""

    with pytest.raises(ValueError, match="saved bridge service"):
        helper.restore(snapshot, runner)
    assert calls == []


def test_managed_recovery_does_not_claim_success_without_bridge_membership(
    helper: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(helper.time, "sleep", lambda _delay: None)
    snapshot = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )

    def runner(argv: list[str]) -> str:
        if argv == ["/sbin/ifconfig", "bridge0"]:
            return "bridge0: flags=<UP,RUNNING>\ninet 192.168.100.11 netmask 0xffffff00\n"
        return ""

    with pytest.raises(ValueError, match="did not restore"):
        helper.reactivate_saved_bridge(snapshot, runner)


@pytest.mark.parametrize("action", ["--apply", "--repair-route"])
def test_unverified_trial_cli_is_suspended_before_any_network_operation(
    helper: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    monkeypatch.setattr(helper.platform, "node", lambda: "coire-edge-a.lab")
    monkeypatch.setattr(helper.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sys, "argv", ["studio-rdma-fabric.py", action, "--interface", "en5"])

    def forbidden(_argv: list[str]) -> str:
        pytest.fail("suspended trial must not run a network command")

    monkeypatch.setattr(helper, "run", forbidden)
    with pytest.raises(SystemExit) as caught:
        helper.main()
    assert caught.value.code == 2
    assert "suspended" in capsys.readouterr().err


@pytest.mark.parametrize("bridge_up", [True, False])
def test_generation_requires_direct_data_route_and_bridge_down(
    helper: Any, monkeypatch: pytest.MonkeyPatch, bridge_up: bool
) -> None:
    monkeypatch.setattr(
        helper.socket,
        "gethostbyname",
        lambda host: "192.168.100.11" if "edge-a" in host else "192.168.100.12",
    )

    def runner(argv: list[str]) -> str:
        assert "coire-edge-a.fabric" in argv
        if argv[-1] == "bridge0":
            return "bridge0: flags=<UP,RUNNING>" if bridge_up else "bridge0: flags=<RUNNING>"
        if argv[-1] == "en5":
            return "inet 192.168.100.11 netmask 0xffffff00\n"
        return "destination: 192.168.100.12\ninterface: en5\nflags: <UP,HOST,DONE,STATIC>\n"

    if bridge_up:
        with pytest.raises(ValueError, match="cutover"):
            helper.check_direct_endpoint("coire-edge-a", "en5", runner)
    else:
        helper.check_direct_endpoint("coire-edge-a", "en5", runner)


def test_hostfile_keeps_native_device_fields_and_pins_data_endpoints(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(helper.platform, "node", lambda: "coire-edge-a.lab")
    monkeypatch.setattr(helper.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        helper.socket,
        "gethostbyname",
        lambda host: "192.168.100.11" if "edge-a" in host else "192.168.100.12",
    )
    observed = []
    monkeypatch.setattr(
        helper, "check_direct_endpoint", lambda host, iface: observed.append((host, iface))
    )
    native = SimpleNamespace(
        Host=lambda rank, ssh, ips, rdma: SimpleNamespace(rank=rank, ssh_hostname=ssh),
        extract_connectivity=lambda _hosts, _verbose: ([], {}),
        make_connectivity_matrix=lambda _topology, _reverse: [],
        check_valid_mesh=lambda _hosts, _connectivity: None,
        IPConfigurator=lambda _hosts, _topology, _reverse: SimpleNamespace(
            ips={(0, 1): [("en6", "192.168.0.1")], (1, 0): [("en5", "192.168.0.2")]}
        ),
        SSHInfo=lambda _ssh, _sudo: None,
        check_rdma=lambda _hosts, _verbose: None,
    )

    def native_generate(args: Any, hosts: Any, fabric: Any, _sshinfo: Any) -> None:
        fabric.setup(auto_setup=False)
        payload = {
            "backend": "jaccl",
            "envs": [],
            "hosts": [
                {
                    "ssh": host.ssh_hostname,
                    "ips": ["192.168.4.11"],
                    "rdma": [None, "native-device-a"]
                    if host.rank == 0
                    else ["native-device-b", None],
                }
                for host in hosts
            ],
        }
        Path(args.output_hostfile).write_text(json.dumps(payload))

    native.configure_jaccl = native_generate
    package = ModuleType("mlx._distributed_utils")
    package.__dict__["config"] = native
    monkeypatch.setitem(sys.modules, "mlx._distributed_utils", package)
    output = tmp_path / "jaccl.json"
    helper.generate_hostfile("jaccl", output)
    payload = json.loads(output.read_text())
    assert payload["hosts"][0]["rdma"] == [None, "native-device-a"]
    assert payload["hosts"][1]["rdma"] == ["native-device-b", None]
    assert [entry["ips"] for entry in payload["hosts"]] == [["192.168.100.11"], ["192.168.100.12"]]
    assert observed == [("coire-edge-a", "en6"), ("coire-edge-b", "en5")] * 2
    with pytest.raises(ValueError, match="already exists"):
        helper.generate_hostfile("jaccl", output)


def test_cutover_does_not_rely_on_a_transient_connected_subnet_route(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    monkeypatch.setattr(helper, "restore", lambda *_args: None)
    monkeypatch.setattr(helper.time, "sleep", lambda _delay: None)
    # macOS can withdraw the automatically-created subnet route after the first
    # successful verification. A static, exact peer route must survive that event.
    static_routes: dict[str, str] = {}
    route_reads = 0
    calls = []

    def runner(argv: list[str]) -> str:
        nonlocal route_reads
        calls.append(argv)
        if argv == ["/sbin/ifconfig", "en5"]:
            return "en5: flags=<UP,RUNNING>\nether 36:f9:75:b7:65:cc\n inet 192.168.100.11 netmask 0xffffff00\n"
        if argv == ["/sbin/ifconfig", "bridge0"]:
            return "bridge0: flags=<RUNNING>\n"
        if argv[0] == "/sbin/route" and "add" in argv:
            static_routes[argv[4]] = argv[-1]
            return ""
        if argv[0] == "/sbin/route" and "get" in argv:
            route_reads += 1
            if argv[-1] in static_routes:
                return f"destination: {argv[-1]}\ninterface: {static_routes[argv[-1]]}\nflags: <UP,HOST,DONE,STATIC>\n"
            if route_reads == 1:
                return "destination: 192.168.100\ninterface: en5\nflags: <UP,DONE>\n"
            return "destination: default\ninterface: en1\nflags: <UP,GATEWAY,STATIC>\n"
        if argv[0].endswith("arp"):
            return "? (192.168.100.12) at 36:b4:6b:e:32:cc on en5 [ethernet]\n"
        return ""

    helper.apply(baseline, tmp_path / "baseline.json", runner)
    assert static_routes == {"192.168.100.11": "lo0", "192.168.100.12": "en5"}
    assert [
        "/sbin/route",
        "-n",
        "add",
        "-host",
        "192.168.100.12",
        "-interface",
        "192.168.100.11",
        "-ifp",
        "en5",
    ] in calls
    assert "default" not in str(calls)


@pytest.mark.parametrize(
    "route",
    [
        "destination: default\ninterface: en1\nflags: <UP,GATEWAY,STATIC>\n",
        "destination: 192.168.100.12\ninterface: en5\nflags: <UP,HOST,DONE>\n",
        "destination: 192.168.100.12\ninterface: en1\nflags: <UP,HOST,STATIC>\n",
        "destination: 192.168.100.12\ninterface: en5\nflags: <UP,HOST,GATEWAY,STATIC>\n",
    ],
)
def test_static_route_proof_rejects_default_transient_wrong_interface_and_gateway(
    helper: Any, route: str
) -> None:
    snapshot = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    assert not helper.peer_route_matches(route, snapshot, static=True)


def test_repair_retains_baseline_and_does_not_replay_address_cutover(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    path = tmp_path / "baseline.json"
    helper.save_snapshot(path, snapshot)
    original = path.read_bytes()
    monkeypatch.setattr(helper.socket, "gethostbyname", lambda _host: snapshot.peer)
    monkeypatch.setattr(helper.time, "sleep", lambda _delay: None)
    installed: dict[str, str] = {}
    calls = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if argv == ["/sbin/ifconfig", "en5"]:
            return "ether 36:f9:75:b7:65:cc\ninet 192.168.100.11 netmask 0xffffff00\n"
        if argv == ["/sbin/ifconfig", "bridge0"]:
            return "bridge0: flags=<RUNNING>\n"
        if "add" in argv:
            installed[argv[4]] = argv[-1]
        if "get" in argv:
            return (
                f"destination: {argv[-1]}\ninterface: {installed[argv[-1]]}\nflags: <UP,HOST,STATIC>\n"
                if argv[-1] in installed
                else "destination: default\ninterface: en1\nflags: <UP,GATEWAY,STATIC>\n"
            )
        if argv[0].endswith("arp"):
            return "? (192.168.100.12) at 36:b4:6b:e:32:cc on en5 [ethernet]\n"
        return ""

    helper.repair_peer_route(helper.load_snapshot(path, "coire-edge-a"), runner)
    helper.repair_peer_route(helper.load_snapshot(path, "coire-edge-a"), runner)
    assert path.read_bytes() == original
    assert sum("add" in call for call in calls) == 2
    assert all(len(call) == 2 for call in calls if call[0] == "/sbin/ifconfig")


def test_unexpected_existing_peer_host_route_is_never_overwritten(helper: Any) -> None:
    snapshot = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    calls = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return "destination: 192.168.100.12\ninterface: en1\nflags: <UP,HOST,STATIC>\n"

    with pytest.raises(ValueError, match="unexpected peer"):
        helper.install_peer_route(snapshot, runner)
    assert len(calls) == 1 and "get" in calls[0]


def test_legacy_baseline_remains_readable_and_is_not_rewritten(helper: Any, tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    content = json.dumps(
        {
            "host": "coire-edge-a",
            "interface": "en5",
            "address": "192.168.100.11",
            "peer": "192.168.100.12",
            "netmask": "255.255.255.0",
            "schema_version": 1,
        }
    )
    path.write_text(content)
    snapshot = helper.load_snapshot(path, "coire-edge-a")
    assert snapshot.local_route_was_static is False
    assert snapshot.peer_route_was_static is False
    assert path.read_text() == content


def test_ethernet_peer_route_repairs_known_self_mac_binding(helper: Any) -> None:
    snapshot = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )
    calls: list[list[str]] = []
    installed = True
    neighbor = "36:f9:75:b7:65:cc"

    def runner(argv: list[str]) -> str:
        nonlocal installed, neighbor
        calls.append(argv)
        if argv == ["/sbin/ifconfig", "en5"]:
            return "ether 36:f9:75:b7:65:cc\ninet 192.168.100.11 netmask 0xffffff00\n"
        if argv[0].endswith("arp"):
            return f"? (192.168.100.12) at {neighbor} on en5 permanent [ethernet]\n"
        if "get" in argv:
            return (
                "destination: 192.168.100.12\ninterface: en5\nflags: <UP,HOST,LLINFO,STATIC>\n"
                if installed
                else "destination: default\ninterface: en1\nflags: <UP,GATEWAY,STATIC>\n"
            )
        if "delete" in argv:
            installed = False
        if "add" in argv:
            assert argv == [
                "/sbin/route",
                "-n",
                "add",
                "-host",
                snapshot.peer,
                "-interface",
                snapshot.address,
                "-ifp",
                "en5",
            ]
            installed = True
            neighbor = "36:b4:6b:0e:32:cc"
        return ""

    helper.install_peer_route(snapshot, runner)
    assert ["/sbin/route", "-n", "delete", "-host", snapshot.peer] in calls
    assert neighbor != "36:f9:75:b7:65:cc"


@pytest.mark.parametrize(
    "arp",
    [
        "? (192.168.100.12) at 36:f9:75:b7:65:cc on en5 permanent [ethernet]\n",
        "? (192.168.100.12) at (incomplete) on en5 [ethernet]\n",
        "? (192.168.100.12) at 36:b4:6b:e:32:cc on en1 [ethernet]\n",
    ],
)
def test_correct_route_flags_cannot_hide_missing_or_wrong_peer_neighbor(
    helper: Any, arp: str
) -> None:
    snapshot = helper.Snapshot(
        "coire-edge-a", "en5", "192.168.100.11", "192.168.100.12", "255.255.255.0"
    )

    def runner(argv: list[str]) -> str:
        if argv == ["/sbin/ifconfig", "en5"]:
            return "ether 36:f9:75:b7:65:cc\ninet 192.168.100.11 netmask 0xffffff00\n"
        if argv == ["/sbin/ifconfig", "bridge0"]:
            return "bridge0: flags=<RUNNING>\n"
        if "get" in argv:
            device = "lo0" if argv[-1] == snapshot.address else "en5"
            return f"destination: {argv[-1]}\ninterface: {device}\nflags: <UP,HOST,STATIC>\n"
        if argv[0].endswith("arp"):
            return arp
        return ""

    with pytest.raises(ValueError, match="neighbor"):
        helper.verify_direct(snapshot, runner)


def test_bad_baseline_static_neighbor_is_refused_without_mutation(helper: Any) -> None:
    snapshot = helper.Snapshot(
        "coire-edge-a",
        "en5",
        "192.168.100.11",
        "192.168.100.12",
        "255.255.255.0",
        peer_route_was_static=True,
    )
    calls = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if "get" in argv:
            return "destination: 192.168.100.12\ninterface: en5\nflags: <UP,HOST,STATIC>\n"
        if argv[0].endswith("arp"):
            return "? (192.168.100.12) at 36:f9:75:b7:65:cc on en5 permanent [ethernet]\n"
        return "ether 36:f9:75:b7:65:cc\n"

    with pytest.raises(ValueError, match="baseline static"):
        helper.install_peer_route(snapshot, runner)
    assert not any("add" in call or "delete" in call or "change" in call for call in calls)
