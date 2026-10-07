#!/usr/bin/env python3
"""Endpoint-preserving, snapshot-backed Studio RDMA cutover (runtime trial).

Run over the control fabric. The interface must come from native MLX discovery.
Persistent macOS network-service changes are a separate, measured release step.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import plistlib
import re
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

NODES = ("coire-edge-a", "coire-edge-b")
STATE = Path("/opt/coire/state/rdma-fabric")
type Runner = Callable[[list[str]], str]


def run(argv: list[str]) -> str:
    try:
        return subprocess.run(
            argv, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15
        ).stdout
    except subprocess.CalledProcessError as error:
        # These deployment commands contain no credentials. Preserve the actual
        # bounded OS diagnostic rather than reporting only a nonzero exit code.
        error.add_note("Network command stderr: " + (error.stderr or "")[:4096])
        raise


@dataclass(frozen=True)
class Snapshot:
    host: str
    interface: str
    address: str
    peer: str
    netmask: str
    schema_version: int = 1
    local_route_was_static: bool = False
    peer_route_was_static: bool = False

    def validate(self) -> None:
        if self.schema_version != 1 or self.host not in NODES:
            raise ValueError("snapshot is not a declared Studio")
        if (
            type(self.local_route_was_static) is not bool
            or type(self.peer_route_was_static) is not bool
        ):
            raise ValueError("snapshot route provenance must be boolean")
        if re.fullmatch(r"en[0-9]{1,2}", self.interface) is None:
            raise ValueError("invalid native interface identity")
        network = ipaddress.IPv4Network(f"{self.address}/{self.netmask}", strict=False)
        local = ipaddress.IPv4Address(self.address)
        peer = ipaddress.IPv4Address(self.peer)
        if (
            not local.is_private
            or local.is_loopback
            or peer == local
            or peer not in network
            or network.prefixlen < 24
            or local in (network.network_address, network.broadcast_address)
            or peer in (network.network_address, network.broadcast_address)
        ):
            raise ValueError("snapshot must preserve a private, isolated Studio peer subnet")


def inet_addresses(output: str) -> list[tuple[str, str]]:
    return re.findall(r"^\s*inet ([0-9.]+) netmask (0x[0-9a-f]+|[0-9.]+)", output, re.M)


def has_endpoint(output: str, snapshot: Snapshot) -> bool:
    for address, mask in inet_addresses(output):
        normalized = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith("0x") else mask
        if address == snapshot.address and normalized == snapshot.netmask:
            return True
    return False


def capture(host: str, interface: str, runner: Runner = run) -> Snapshot:
    if host not in NODES or re.fullmatch(r"en[0-9]{1,2}", interface) is None:
        raise ValueError("expected a declared Studio and a native-discovered interface")
    bridge = runner(["/sbin/ifconfig", "bridge0"])
    target = runner(["/sbin/ifconfig", interface])
    if "<UP," not in bridge or "status: active" not in target:
        raise ValueError("the existing bridge and native direct link must be active")
    if re.search(rf"\bmember: {re.escape(interface)}\b", bridge) is None:
        raise ValueError("native interface is not a member of the current data bridge")
    addresses = inet_addresses(bridge)
    if len(addresses) != 1 or inet_addresses(target):
        raise ValueError("refusing ambiguous bridge/interface IPv4 configuration")
    address, mask = addresses[0]
    netmask = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith("0x") else mask
    peer_host = next(node for node in NODES if node != host)
    peer = socket.gethostbyname(peer_host + ".fabric")
    local_route = runner(["/sbin/route", "-n", "get", address])
    peer_route = runner(["/sbin/route", "-n", "get", peer])
    snapshot = Snapshot(
        host,
        interface,
        address,
        peer,
        netmask,
        local_route_was_static=host_route_matches(local_route, address, "lo0", static=True),
        peer_route_was_static=host_route_matches(peer_route, peer, interface, static=True),
    )
    snapshot.validate()
    if socket.gethostbyname(host + ".fabric") != address:
        raise ValueError("bridge address differs from the declared replication endpoint")
    return snapshot


def commands(snapshot: Snapshot, *, rollback: bool) -> list[list[str]]:
    snapshot.validate()
    if rollback:
        return [
            ["/sbin/ifconfig", snapshot.interface, "inet", snapshot.address, "-alias"],
            ["/usr/sbin/networksetup", "-setnetworkserviceenabled", "Thunderbolt Bridge", "off"],
            ["/usr/sbin/networksetup", "-setnetworkserviceenabled", "Thunderbolt Bridge", "on"],
        ]
    return [
        ["/sbin/ifconfig", "bridge0", "down"],
        ["/sbin/ifconfig", "bridge0", "inet", snapshot.address, "-alias"],
        ["/sbin/ifconfig", "bridge0", "deletem", snapshot.interface],
        [
            "/sbin/ifconfig",
            snapshot.interface,
            "inet",
            snapshot.address,
            "netmask",
            snapshot.netmask,
            "up",
        ],
    ]


def save_snapshot(path: Path, snapshot: Snapshot) -> None:
    """Publish baseline once; never overwrite the only rollback record."""
    snapshot.validate()
    if path.parent.is_symlink():
        raise ValueError("snapshot directory must not be linked")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError("baseline already exists; inspect or roll back before a new trial")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".baseline-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(asdict(snapshot), output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        # Hard-link publication refuses an existing destination atomically.
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load_snapshot(path: Path, host: str) -> Snapshot:
    if (
        path.parent.is_symlink()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 4096
    ):
        raise ValueError("rollback baseline is missing or unsafe")
    snapshot = Snapshot(**json.loads(path.read_text()))
    snapshot.validate()
    if snapshot.host != host:
        raise ValueError("rollback baseline belongs to a different Studio")
    return snapshot


def host_route_matches(output: str, address: str, device: str, *, static: bool) -> bool:
    destination = re.search(r"^\s*destination:\s*(\S+)", output, re.M)
    interface = re.search(r"^\s*interface:\s*(\S+)", output, re.M)
    flags = re.search(r"^\s*flags:\s*<([^>]+)>", output, re.M)
    if (
        destination is None
        or destination[1] != address
        or interface is None
        or interface[1] != device
        or (flags is not None and "GATEWAY" in flags[1].split(","))
    ):
        return False
    return not static or (flags is not None and {"HOST", "STATIC"} <= set(flags[1].split(",")))


def peer_route_matches(output: str, snapshot: Snapshot, *, static: bool) -> bool:
    return host_route_matches(output, snapshot.peer, snapshot.interface, static=static)


def mac_words(value: str) -> tuple[int, ...]:
    if re.fullmatch(r"[0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5}", value) is None:
        raise ValueError("invalid Ethernet address")
    return tuple(int(word, 16) for word in value.split(":"))


def neighbor_macs(
    snapshot: Snapshot, runner: Runner = run
) -> tuple[tuple[int, ...], tuple[int, ...] | None]:
    target = runner(["/sbin/ifconfig", snapshot.interface])
    local = re.search(r"\bether\s+(\S+)", target)
    if local is None:
        raise ValueError("native data interface has no observed Ethernet address")
    try:
        arp = runner(["/usr/sbin/arp", "-n", snapshot.peer])
    except subprocess.CalledProcessError:
        arp = ""
    neighbor = re.search(
        rf"\({re.escape(snapshot.peer)}\)\s+at\s+([0-9a-fA-F:]+)\s+on\s+{re.escape(snapshot.interface)}\b",
        arp,
    )
    return mac_words(local[1]), mac_words(neighbor[1]) if neighbor else None


def peer_route_argv(snapshot: Snapshot, operation: str) -> list[str]:
    snapshot.validate()
    if operation not in {"add", "change"}:
        raise ValueError("unsupported peer route operation")
    # Ethernet route(8) requires this host's address as the -interface gateway.
    # An interface *name* is the point-to-point form; on Ethernet it can create
    # a permanent neighbor entry mapping the peer to this host's own MAC.
    argv = [
        "/sbin/route",
        "-n",
        operation,
        "-host",
        snapshot.peer,
        "-interface",
        snapshot.address,
        "-ifp",
        snapshot.interface,
    ]
    if operation == "change":
        argv.append("-static")
    return argv


def install_local_route(snapshot: Snapshot, runner: Runner = run) -> None:
    """Keep the node's own declared address local, including native self-SSH."""
    snapshot.validate()
    route = runner(["/sbin/route", "-n", "get", snapshot.address])
    if host_route_matches(route, snapshot.address, "lo0", static=True):
        return
    if host_route_matches(route, snapshot.address, "lo0", static=False):
        runner(
            [
                "/sbin/route",
                "-n",
                "change",
                "-host",
                snapshot.address,
                "-interface",
                "lo0",
                "-static",
            ]
        )
    else:
        destination = re.search(r"^\s*destination:\s*(\S+)", route, re.M)
        if destination and destination[1] == snapshot.address:
            raise ValueError("refusing to replace an unexpected local host route")
        runner(["/sbin/route", "-n", "add", "-host", snapshot.address, "-interface", "lo0"])


def install_peer_route(snapshot: Snapshot, runner: Runner = run) -> None:
    """Pin only this declared peer; do not depend on macOS's transient subnet route."""
    snapshot.validate()
    route = runner(["/sbin/route", "-n", "get", snapshot.peer])
    if peer_route_matches(route, snapshot, static=True):
        local_mac, peer_mac = neighbor_macs(snapshot, runner)
        if peer_mac != local_mac:
            return
        if snapshot.peer_route_was_static:
            raise ValueError("refusing to replace a baseline static route with a self-MAC binding")
        # Replace only the observed defective route from this helper's earlier
        # trial. A host route on another interface remains a conflict below.
        runner(["/sbin/route", "-n", "delete", "-host", snapshot.peer])
        runner(peer_route_argv(snapshot, "add"))
        return
    if peer_route_matches(route, snapshot, static=False):
        # Convert only a directly-connected neighbor on this interface. Never
        # change the default route or an administrator's host route elsewhere.
        runner(peer_route_argv(snapshot, "change"))
    else:
        destination = re.search(r"^\s*destination:\s*(\S+)", route, re.M)
        if destination and destination[1] == snapshot.peer:
            raise ValueError("refusing to replace an unexpected peer host route")
        runner(peer_route_argv(snapshot, "add"))


def verify_direct(snapshot: Snapshot, runner: Runner = run) -> None:
    # ICMP primes ARP even while the peer's route correction is still pending.
    # A failed echo is not a fabric pass; actual authenticated TCP verification
    # is required separately after both participants finish the cutover.
    with suppress(subprocess.CalledProcessError, subprocess.TimeoutExpired):
        runner(["/sbin/ping", "-c", "1", "-t", "1", "-S", snapshot.address, snapshot.peer])
    # Sample after installation and again after macOS has had time to reconcile
    # network services. An immediate successful connected route is insufficient.
    for sample in range(5):
        if sample:
            time.sleep(2)
        target = runner(["/sbin/ifconfig", snapshot.interface])
        bridge = runner(["/sbin/ifconfig", "bridge0"])
        route = runner(["/sbin/route", "-n", "get", snapshot.peer])
        local_route = runner(["/sbin/route", "-n", "get", snapshot.address])
        local_mac, peer_mac = neighbor_macs(snapshot, runner)
        if (
            not has_endpoint(target, snapshot)
            or "<UP," in bridge
            or not peer_route_matches(route, snapshot, static=True)
            or not host_route_matches(local_route, snapshot.address, "lo0", static=True)
            or peer_mac is None
            or peer_mac == local_mac
        ):
            raise ValueError("direct endpoint/static route/peer neighbor verification failed")


def read_network_preferences() -> dict[str, Any]:
    path = Path("/Library/Preferences/SystemConfiguration/preferences.plist")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024**2:
        raise ValueError("saved macOS network preferences are absent or unsafe")
    value = plistlib.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("saved macOS network preferences are invalid")
    return value


def require_saved_bridge(snapshot: Snapshot) -> None:
    """Refuse recovery if the administrator's persistent configuration changed."""
    snapshot.validate()
    preferences = read_network_preferences()
    bridge = preferences.get("VirtualNetworkInterfaces", {}).get("Bridge", {}).get("bridge0", {})
    services = [
        service
        for service in preferences.get("NetworkServices", {}).values()
        if service.get("UserDefinedName") == "Thunderbolt Bridge"
    ]
    if len(services) != 1 or snapshot.interface not in bridge.get("Interfaces", []):
        raise ValueError("saved bridge membership does not match the retained baseline")
    service = services[0]
    ipv4 = service.get("IPv4", {})
    if (
        service.get("Interface", {}).get("DeviceName") != "bridge0"
        or service.get("__INACTIVE__")
        or ipv4.get("ConfigMethod") != "Manual"
        or ipv4.get("Addresses") != [snapshot.address]
        or ipv4.get("SubnetMasks") != [snapshot.netmask]
        or ipv4.get("Router") not in (None, "")
    ):
        raise ValueError("saved bridge service differs from the original isolated endpoint")


def reactivate_saved_bridge(snapshot: Snapshot, runner: Runner = run) -> None:
    require_saved_bridge(snapshot)
    tool = "/usr/sbin/networksetup"
    try:
        runner([tool, "-setnetworkserviceenabled", "Thunderbolt Bridge", "off"])
    finally:
        # Always request the originally enabled state, including when disable
        # fails or is interrupted. Do not rewrite the service's address/router.
        runner([tool, "-setnetworkserviceenabled", "Thunderbolt Bridge", "on"])
    for sample in range(5):
        if sample:
            time.sleep(2)
        bridge = runner(["/sbin/ifconfig", "bridge0"])
        target = runner(["/sbin/ifconfig", snapshot.interface])
        if (
            has_endpoint(bridge, snapshot)
            and "<UP," in bridge
            and re.search(rf"\bmember: {re.escape(snapshot.interface)}\b", bridge)
            and not any(ip == snapshot.address for ip, _mask in inet_addresses(target))
        ):
            return
    raise ValueError(
        "managed service reactivation did not restore the bridge; further mutation stopped"
    )


def recover_unaddressed_bridge(host: str, interface: str, runner: Runner = run) -> None:
    """Use the existing OS-managed service after a partial rollback removed IPv4.

    networksetup enforces its own administrator authorization. This path performs
    no kernel route/alias mutations and refuses any uncleared trial state.
    """
    preferences = read_network_preferences()
    services = [
        service
        for service in preferences.get("NetworkServices", {}).values()
        if service.get("UserDefinedName") == "Thunderbolt Bridge"
    ]
    if len(services) != 1:
        raise ValueError("expected one existing Thunderbolt Bridge service")
    ipv4 = services[0].get("IPv4", {})
    addresses, masks = ipv4.get("Addresses", []), ipv4.get("SubnetMasks", [])
    if len(addresses) != 1 or len(masks) != 1 or host not in NODES:
        raise ValueError("saved endpoint is ambiguous")
    peer_host = next(node for node in NODES if node != host)
    snapshot = Snapshot(
        host, interface, addresses[0], socket.gethostbyname(peer_host + ".fabric"), masks[0]
    )
    require_saved_bridge(snapshot)
    if socket.gethostbyname(host + ".fabric") != snapshot.address:
        raise ValueError("saved endpoint differs from declared DNS")
    if inet_addresses(runner(["/sbin/ifconfig", interface])):
        raise ValueError("direct-interface address still requires baseline rollback cleanup")
    for address in (snapshot.address, snapshot.peer):
        route = runner(["/sbin/route", "-n", "get", address])
        if host_route_matches(
            route, address, "lo0" if address == snapshot.address else interface, static=True
        ):
            raise ValueError("trial host route still requires baseline rollback cleanup")
    reactivate_saved_bridge(snapshot, runner)


def restore(snapshot: Snapshot, runner: Runner = run) -> None:
    """Recover even after failure between any two cutover operations."""
    snapshot.validate()
    require_saved_bridge(snapshot)
    route = runner(["/sbin/route", "-n", "get", snapshot.peer])
    if not snapshot.peer_route_was_static and peer_route_matches(route, snapshot, static=True):
        runner(["/sbin/route", "-n", "delete", "-host", snapshot.peer])
    local_route = runner(["/sbin/route", "-n", "get", snapshot.address])
    if not snapshot.local_route_was_static and host_route_matches(
        local_route, snapshot.address, "lo0", static=True
    ):
        runner(["/sbin/route", "-n", "delete", "-host", snapshot.address])
    target = runner(["/sbin/ifconfig", snapshot.interface])
    if any(address == snapshot.address for address, _mask in inet_addresses(target)):
        runner(commands(snapshot, rollback=True)[0])
    # macOS rejected raw `bridge0 addm en5` on the real host. Reconcile using
    # the unchanged, supported OS network service instead of retrying that ioctl.
    reactivate_saved_bridge(snapshot, runner)


def apply(snapshot: Snapshot, path: Path, runner: Runner = run) -> None:
    save_snapshot(path, snapshot)
    try:
        for argv in commands(snapshot, rollback=False):
            runner(argv)
        install_local_route(snapshot, runner)
        install_peer_route(snapshot, runner)
        verify_direct(snapshot, runner)
    except BaseException:
        restore(snapshot, runner)
        raise


def repair_peer_route(snapshot: Snapshot, runner: Runner = run) -> None:
    """Complete an existing trial using its original immutable rollback baseline."""
    snapshot.validate()
    target = runner(["/sbin/ifconfig", snapshot.interface])
    bridge = runner(["/sbin/ifconfig", "bridge0"])
    if not has_endpoint(target, snapshot) or "<UP," in bridge:
        raise ValueError(
            "route repair requires the original address already on the direct interface"
        )
    peer_host = next(node for node in NODES if node != snapshot.host)
    if socket.gethostbyname(peer_host + ".fabric") != snapshot.peer:
        raise ValueError("declared peer differs from the retained baseline")
    try:
        install_local_route(snapshot, runner)
        install_peer_route(snapshot, runner)
        verify_direct(snapshot, runner)
    except BaseException:
        restore(snapshot, runner)
        raise


def check_direct_endpoint(host: str, interface: str, runner: Runner = run) -> None:
    """Run read-only checks on a peer through its declared data hostname."""
    if host not in NODES or re.fullmatch(r"en[0-9]{1,2}", interface) is None:
        raise ValueError("invalid discovered peer/interface")
    ssh = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host + ".fabric"]
    bridge = runner([*ssh, "/sbin/ifconfig", "bridge0"])
    target = runner([*ssh, "/sbin/ifconfig", interface])
    address = socket.gethostbyname(host + ".fabric")
    if "<UP," in bridge or not any(ip == address for ip, _mask in inet_addresses(target)):
        raise ValueError(f"{host} has not completed the endpoint-preserving cutover")
    peer = next(node for node in NODES if node != host)
    route = runner([*ssh, "/sbin/route", "-n", "get", peer + ".fabric"])
    _address, mask = next(item for item in inet_addresses(target) if item[0] == address)
    netmask = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith("0x") else mask
    snapshot = Snapshot(host, interface, address, socket.gethostbyname(peer + ".fabric"), netmask)
    snapshot.validate()
    if not peer_route_matches(route, snapshot, static=True):
        raise ValueError("static peer route is not on the native-discovered data interface")


def check_bridge_endpoint(host: str, interface: str, runner: Runner = run) -> None:
    """Validate the existing data bridge, without any interface/route mutation.

    RDMA device identity still comes from native Thunderbolt discovery. The
    bridge supplies only the existing IP side channel and replication endpoint.
    """
    if host not in NODES or re.fullmatch(r"en[0-9]{1,2}", interface) is None:
        raise ValueError("invalid discovered peer/interface")
    ssh = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host + ".fabric"]
    bridge = runner([*ssh, "/sbin/ifconfig", "bridge0"])
    target = runner([*ssh, "/sbin/ifconfig", interface])
    address = socket.gethostbyname(host + ".fabric")
    peer = next(node for node in NODES if node != host)
    # Match the exact generated /30 owned by studio-rdma-alias.py. An unrelated
    # direct-interface address must still refuse bridge-preserving generation.
    slot = int.from_bytes(hashlib.sha256(b"coire-rdma-linklocal-v1").digest()[:2], "big") % 16128
    alias = str(
        ipaddress.IPv4Address(
            int(ipaddress.IPv4Address("169.254.1.0")) + slot * 4 + NODES.index(host) + 1
        )
    )
    target_addresses = inet_addresses(target)
    approved_alias = (
        len(target_addresses) == 1
        and target_addresses[0][0] == alias
        and target_addresses[0][1] in {"0xfffffffc", "255.255.255.252"}
    )
    if (
        "<UP," not in bridge
        or not re.search(rf"\bmember:\s+{re.escape(interface)}\s", bridge)
        or not any(ip == address for ip, _mask in inet_addresses(bridge))
        or (target_addresses and not approved_alias)
        or not re.search(r"\bstatus:\s+active\b", target)
    ):
        raise ValueError("declared bridge endpoint/native membership is not active")
    _address, mask = next(item for item in inet_addresses(bridge) if item[0] == address)
    netmask = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith("0x") else mask
    snapshot = Snapshot(host, interface, address, socket.gethostbyname(peer + ".fabric"), netmask)
    snapshot.validate()
    route = runner([*ssh, "/sbin/route", "-n", "get", peer + ".fabric"])
    if not re.search(r"\binterface:\s+bridge0(?:\s|$)", route):
        raise ValueError("declared peer route is not on the existing data bridge")


def generate_hostfile(backend: str, output: Path, *, bridge: bool = False) -> None:
    """Let pinned native MLX generate all topology/device fields on the existing subnet.

    The native routine's network setup object is supplied explicitly. It validates
    our already-applied configuration instead of readdressing it to MLX's default
    /30. No MLX function/global is patched; no RDMA device field is hand-authored.
    """
    if platform.node().split(".", 1)[0] != "coire-edge-a" or platform.system() != "Darwin":
        raise ValueError("native discovery runs only on Studio A")
    if backend not in {"jaccl", "ring"} or not output.parent.is_dir():
        raise ValueError("expected a supported backend and existing output directory")
    if output.exists() or output.is_symlink():
        raise ValueError("hostfile already exists; preserve it before generating a new version")
    from mlx._distributed_utils import config as native_config

    native: Any = native_config

    hosts = [native.Host(i, node + ".fabric", [], []) for i, node in enumerate(NODES)]
    topology, reverse = native.extract_connectivity(hosts, False)
    native.check_valid_mesh(hosts, native.make_connectivity_matrix(topology, reverse))
    discovered = native.IPConfigurator(hosts, topology, reverse)
    validate_endpoint = check_bridge_endpoint if bridge else check_direct_endpoint

    class ExistingFabric:
        def __init__(self) -> None:
            self.ips: dict[tuple[int, int], list[tuple[str, str]]] = {}
            for (source, peer), links in discovered.ips.items():
                if len(links) != 1:
                    raise ValueError("expected one direct Thunderbolt link per Studio pair")
                interface = links[0][0]
                validate_endpoint(NODES[source], interface)
                self.ips[source, peer] = [
                    (interface, socket.gethostbyname(NODES[source] + ".fabric"))
                ]
            if set(self.ips) != {(0, 1), (1, 0)}:
                raise ValueError("native discovery did not find the complete two-Studio link")

        def setup(self, **_kwargs: Any) -> None:
            for (source, _peer), links in self.ips.items():
                validate_endpoint(NODES[source], links[0][0])

    fabric = ExistingFabric()
    fd, name = tempfile.mkstemp(prefix=".native-hostfile-", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        args = SimpleNamespace(
            verbose=False, auto_setup=False, output_hostfile=str(temporary), env=[]
        )
        sshinfo = [native.SSHInfo(True, False) for _node in NODES]
        if backend == "jaccl":
            native.check_rdma(hosts, False)
            native.configure_jaccl(args, hosts, fabric, sshinfo)
        else:
            rings = native.extract_rings(native.make_connectivity_matrix(topology, reverse))
            native.check_valid_ring(hosts, rings)
            native.configure_ring(args, hosts, fabric, rings[0], sshinfo)
        payload = json.loads(temporary.read_text())
        # Native JACCL adds control IPs for its side channel; pin the generated
        # entries to the declared data addresses instead, as ring already does.
        for node, entry in zip(NODES, payload["hosts"], strict=True):
            if entry["ssh"] != node + ".fabric":
                raise ValueError("native hostfile rank order differs from declared inventory")
            entry["ips"] = [socket.gethostbyname(node + ".fabric")]
        with temporary.open("w") as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output, follow_symlinks=False)
        directory = os.open(output.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    action.add_argument("--repair-route", action="store_true")
    action.add_argument("--recover-managed", action="store_true")
    action.add_argument("--generate", choices=["jaccl", "ring"])
    parser.add_argument("--interface", help="interface obtained from native MLX discovery")
    parser.add_argument("--output", type=Path, help="new native hostfile destination")
    parser.add_argument(
        "--generate-on-bridge",
        action="store_true",
        help="validate the restored bridge as the unchanged IP side channel during generation",
    )
    args = parser.parse_args()
    if args.generate_on_bridge and not args.generate:
        parser.error("--generate-on-bridge requires --generate")
    host = platform.node().split(".", 1)[0]
    if host not in NODES or platform.system() != "Darwin":
        parser.error("run on a declared Studio, over the control fabric")
    if args.apply or args.repair_route:
        parser.error(
            "direct-interface trials are suspended after failed real-host rollback; recover the saved bridge first"
        )
    if (args.apply or args.rollback or args.repair_route) and os.geteuid() != 0:
        parser.error("application and rollback require operator-authenticated sudo")
    path = STATE / f"{host}.json"
    if args.recover_managed:
        if not args.interface:
            parser.error("--interface from the original trial is required")
        recover_unaddressed_bridge(host, args.interface)
        print("OS-managed bridge state restored; authenticated peer verification remains required")
    elif args.generate:
        if not args.output:
            parser.error("--output is required for native hostfile generation")
        generate_hostfile(args.generate, args.output, bridge=args.generate_on_bridge)
        print("native hostfile generated from validated unchanged data endpoints")
    elif args.repair_route:
        repair_peer_route(load_snapshot(path, host))
        print("local/peer routes and peer Ethernet neighbor verified; original baseline retained")
        print(
            "authenticated peer TCP/collective verification is still required after both hosts finish"
        )
    elif args.rollback:
        restore(load_snapshot(path, host))
        print("runtime bridge restored; baseline retained for operator inspection")
    else:
        if not args.interface:
            parser.error("--interface from native discovery is required")
        snapshot = capture(host, args.interface)
        if args.check:
            print(
                json.dumps(
                    {
                        "baseline": asdict(snapshot),
                        "apply": commands(snapshot, rollback=False),
                        "rollback": commands(snapshot, rollback=True),
                        "peer_route": peer_route_argv(snapshot, "add"),
                        "local_route": [
                            "/sbin/route",
                            "-n",
                            "add",
                            "-host",
                            snapshot.address,
                            "-interface",
                            "lo0",
                        ],
                    },
                    indent=2,
                )
            )
        else:
            apply(snapshot, path)
            print("runtime cutover verified; endpoints preserved; rollback baseline retained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
