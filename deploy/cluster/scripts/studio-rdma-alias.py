#!/usr/bin/env python3
"""Prepare/apply a reversible RDMA IPv4 alias without dismantling the data bridge."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

NODES = ("coire-edge-a", "coire-edge-b")
STATE = Path("/opt/coire/state/rdma-aliases")
type Runner = Callable[[list[str]], str]


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} exited {result.returncode}: {result.stderr.strip()[:512]}")
    return result.stdout


def alias_for(host: str) -> str:
    if host not in NODES:
        raise ValueError("only declared Studios may acquire an RDMA alias")
    # Deterministic generated /30 wholly inside RFC 3927's nonreserved range.
    slot = int.from_bytes(hashlib.sha256(b"coire-rdma-linklocal-v1").digest()[:2], "big") % 16128
    network = int(ipaddress.IPv4Address("169.254.1.0")) + slot * 4
    return str(ipaddress.IPv4Address(network + NODES.index(host) + 1))


def addresses(text: str) -> list[tuple[str, str]]:
    return re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(\S+)", text)


def route_fields(text: str) -> dict[str, str]:
    return dict(re.findall(r"^\s*(destination|gateway|interface|mask):\s*(\S+)", text, re.M))


def capture(host: str, interface: str, runner: Runner = run) -> dict[str, Any]:
    if host not in NODES or re.fullmatch(r"en[0-9]{1,2}", interface) is None:
        raise ValueError("invalid declared node/native interface")
    peer = next(node for node in NODES if node != host)
    bridge = runner(["/sbin/ifconfig", "bridge0"])
    target = runner(["/sbin/ifconfig", interface])
    members = sorted(re.findall(r"\bmember:\s+(en\d+)\s", bridge))
    bridge_addresses = addresses(bridge)
    if (
        "<UP," not in bridge
        or interface not in members
        or not re.search(r"\bstatus:\s+active\b", target)
        or not any(ip == socket.gethostbyname(host + ".fabric") for ip, _ in bridge_addresses)
    ):
        raise ValueError("original declared bridge/native membership is not active")
    data_route = route_fields(runner(["/sbin/route", "-n", "get", peer + ".fabric"]))
    if data_route.get("interface") != "bridge0":
        raise ValueError("declared peer no longer routes on the original data bridge")
    control_route = route_fields(runner(["/sbin/route", "-n", "get", "coire-core.lab"]))
    if control_route.get("interface") in {None, interface, "bridge0"}:
        raise ValueError("control route does not identify the independent control fabric")
    return {
        "schema_version": 1,
        "host": host,
        "interface": interface,
        "alias": alias_for(host),
        "bridge_addresses": bridge_addresses,
        "members": members,
        "data_route": data_route,
        "control_route": control_route,
        "target_addresses": addresses(target),
    }


def unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    keys = (
        "host",
        "interface",
        "alias",
        "bridge_addresses",
        "members",
        "data_route",
        "control_route",
    )
    if any(before[key] != after[key] for key in keys):
        raise ValueError("bridge endpoints/membership or control/data routes changed")


def has_alias(snapshot: dict[str, Any]) -> bool:
    return bool(snapshot["target_addresses"] == [(snapshot["alias"], "0xfffffffc")])


def check_rdma(interface: str, runner: Runner) -> None:
    if "enabled" not in runner(["/usr/bin/rdma_ctl", "status"]).lower():
        raise ValueError("native RDMA capability is not enabled")
    if "PORT_ACTIVE" not in runner(["/usr/bin/ibv_devinfo", "-d", "rdma_" + interface]):
        raise ValueError("native-discovered RDMA port is not active")


def verify_gid(alias: str, interface: str, runner: Runner) -> None:
    value = runner(["/usr/bin/ibv_devinfo", "-v", "-d", "rdma_" + interface])
    for token in re.findall(r"[0-9a-fA-F:.]*:[0-9a-fA-F:.]+", value):
        try:
            mapped = ipaddress.IPv6Address(token).ipv4_mapped
        except ValueError:
            continue
        if mapped is not None and str(mapped) == alias:
            return
    raise ValueError("RDMA still has no mapped GID for the generated alias")


def save(path: Path, snapshot: dict[str, Any]) -> None:
    if any(parent.is_symlink() for parent in (path.parent, *path.parent.parents)):
        raise ValueError("RDMA alias baseline directory is linked")
    path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("RDMA alias baseline directory is not private")
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(snapshot, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("RDMA alias baseline unavailable or unsafe")
    if path.stat().st_mode & 0o077:
        raise ValueError("RDMA alias baseline is not private")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("RDMA alias baseline is invalid")
    if value.get("host") not in NODES or value.get("alias") != alias_for(value["host"]):
        raise ValueError("RDMA alias baseline identity differs")
    value["bridge_addresses"] = [tuple(pair) for pair in value["bridge_addresses"]]
    value["target_addresses"] = [tuple(pair) for pair in value["target_addresses"]]
    if value["target_addresses"]:
        raise ValueError("baseline already has direct-interface IPv4 addresses")
    return value


def rollback(path: Path, runner: Runner = run) -> None:
    baseline = load(path)
    now = capture(baseline["host"], baseline["interface"], runner)
    unchanged(baseline, now)
    if now["target_addresses"] and not has_alias(now):
        raise ValueError("unexpected interface alias; refusing to remove it")
    if has_alias(now):
        runner(["/sbin/ifconfig", baseline["interface"], "inet", baseline["alias"], "-alias"])
    restored = capture(baseline["host"], baseline["interface"], runner)
    unchanged(baseline, restored)
    if restored["target_addresses"]:
        raise ValueError("generated alias removal is unproved")


def apply(
    host: str,
    interface: str,
    path: Path,
    runner: Runner = run,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    before = capture(host, interface, runner)
    check_rdma(interface, runner)
    if before["target_addresses"]:
        raise ValueError("native interface already has an IPv4 address; refusing mutation")
    if path.exists():
        raise ValueError("baseline retained; use rollback before preparing another trial")
    save(path, before)
    try:
        runner(
            [
                "/sbin/ifconfig",
                interface,
                "inet",
                before["alias"],
                "netmask",
                "255.255.255.252",
                "alias",
            ]
        )
        for _ in range(5):
            after = capture(host, interface, runner)
            unchanged(before, after)
            if not has_alias(after):
                raise ValueError("generated interface alias did not remain configured")
            verify_gid(before["alias"], interface, runner)
            sleep(2)
    except BaseException:
        rollback(path, runner)
        raise


def restore(
    host: str,
    interface: str,
    path: Path,
    runner: Runner = run,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Restore only the saved generated alias after boot, without replacing its baseline."""
    baseline = load(path)
    if baseline["host"] != host or baseline["interface"] != interface:
        raise ValueError("RDMA alias baseline identity differs")
    before = capture(host, interface, runner)
    unchanged(baseline, before)
    check_rdma(interface, runner)
    if before["target_addresses"] and not has_alias(before):
        raise ValueError("unexpected interface alias; refusing restoration")
    added = False
    try:
        if not has_alias(before):
            # A failed add may still have changed kernel state, so rollback owns that case too.
            added = True
            runner(
                [
                    "/sbin/ifconfig",
                    interface,
                    "inet",
                    baseline["alias"],
                    "netmask",
                    "255.255.255.252",
                    "alias",
                ]
            )
        for _ in range(5):
            after = capture(host, interface, runner)
            unchanged(baseline, after)
            if not has_alias(after):
                raise ValueError("saved generated alias did not remain configured")
            verify_gid(baseline["alias"], interface, runner)
            sleep(2)
    except BaseException:
        if added:
            rollback(path, runner)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    action.add_argument("--restore", action="store_true")
    parser.add_argument("--interface", required=True, help="interface from native discovery")
    args = parser.parse_args()
    host = platform.node().split(".", 1)[0]
    if platform.system() != "Darwin" or host not in NODES:
        parser.error("run only on the declared Studios")
    path = STATE / f"{host}.json"
    if args.check:
        snapshot = capture(host, args.interface)
        check_rdma(args.interface, run)
        if snapshot["target_addresses"]:
            parser.error("native interface already has IPv4 addresses")
        print(
            json.dumps(
                {
                    "host": host,
                    "interface": args.interface,
                    "generated_alias": snapshot["alias"],
                    "mask": "255.255.255.252",
                    "bridge_preserved": True,
                    "root_required": True,
                },
                sort_keys=True,
            )
        )
    else:
        if os.geteuid() != 0:
            parser.error("alias application/removal requires operator-authenticated sudo")
        if args.restore:
            restore(host, args.interface, path)
            print("saved alias/GID restored; original baseline and bridge/routes preserved")
        elif args.rollback:
            if load(path)["interface"] != args.interface:
                parser.error("interface differs from the saved baseline")
            rollback(path)
            print("generated alias removed; original bridge/control/data state verified")
        else:
            apply(host, args.interface, path)
            print(
                "generated alias/GID and unchanged bridge/routes verified; collective acceptance remains required"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
