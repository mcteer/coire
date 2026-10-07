"""Native hostfile setup can validate the restored bridge without changing it."""

from typing import Any

import pytest
from test_studio_rdma_fabric import helper as helper


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "down",
        "address",
        "member",
        "inactive",
        "route",
        "approved-alias",
        "foreign-alias",
        "alias-mask",
    ],
)
def test_restored_bridge_validation_is_read_only_and_fail_closed(
    helper: Any,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    addresses = {"coire-edge-a.fabric": "192.168.100.11", "coire-edge-b.fabric": "192.168.100.12"}
    monkeypatch.setattr(helper.socket, "gethostbyname", addresses.__getitem__)
    calls: list[list[str]] = []

    def read(argv: list[str]) -> str:
        calls.append(argv)
        assert argv[:6] == [
            "/usr/bin/ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "coire-edge-a.fabric",
        ]
        if argv[6:] == ["/sbin/ifconfig", "bridge0"]:
            state = "DOWN" if fault == "down" else "UP"
            address = "192.168.99.11" if fault == "address" else "192.168.100.11"
            member = "en6" if fault == "member" else "en5"
            return f"bridge0: flags=8843<{state},BROADCAST,RUNNING>\n inet {address} netmask 0xffffff00\n member: {member} flags=3\n status: active\n"
        if argv[6:] == ["/sbin/ifconfig", "en5"]:
            status = "inactive" if fault == "inactive" else "active"
            alias = ""
            if fault in {"approved-alias", "foreign-alias", "alias-mask"}:
                address = "169.254.102.213" if fault == "foreign-alias" else "169.254.102.217"
                mask = "0xffffff00" if fault == "alias-mask" else "0xfffffffc"
                alias = f" inet {address} netmask {mask}\n"
            return f"en5: flags=8843<UP,BROADCAST,RUNNING>\n status: {status}\n{alias}"
        assert argv[6:] == ["/sbin/route", "-n", "get", "coire-edge-b.fabric"]
        interface = "en1" if fault == "route" else "bridge0"
        return f"route to: 192.168.100.12\n interface: {interface}\n"

    if fault in {None, "approved-alias"}:
        helper.check_bridge_endpoint("coire-edge-a", "en5", read)
    else:
        with pytest.raises(ValueError):
            helper.check_bridge_endpoint("coire-edge-a", "en5", read)
    assert all(len(argv) in {8, 10} for argv in calls)
