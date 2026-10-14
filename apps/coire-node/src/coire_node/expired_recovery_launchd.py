"""Positive fixed launchd owner observation; no process signals or bootout."""

import hashlib
import os
import plistlib
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryLegacyService
from coire_core.models.upgrades import NativeGuardStatus
from coire_core.settings import Settings
from coire_node.recovery_command import capture_owned_command


def verify_legacy_definition(
    settings: Settings,
    guard: NativeGuardStatus,
    data: bytes,
    expected_user: str,
) -> str:
    """Trusted installed plist bytes, never a caller-supplied stop command.

    Live loaded service properties must also be checked: disk definition alone
    does not prove launchd loaded it. This evaluator performs no stop or writes.
    """
    if len(data) > 64 * 1024:
        raise UpgradeUnavailable("Legacy guard definition exceeds bound")
    try:
        document = plistlib.loads(data)
    except (ValueError, plistlib.InvalidFileException):
        raise UpgradeUnavailable("Legacy guard definition is unreadable") from None
    expected = [
        str(
            Path(settings.upgrade_environment_root)
            / guard.bootstrap_runtime.environment_id
            / "bin/python3"
        ),
        "-I",
        "-B",
        "-m",
        "coire_node.upgrade_guard",
    ]
    if (
        not isinstance(document, dict)
        or document.get("Label") != "com.coire.upgrade-guard"
        or document.get("UserName") != expected_user
        or document.get("ProgramArguments") != expected
        or document.get("AbandonProcessGroup") is not True
        or guard.node != settings.node_name
    ):
        raise UpgradeUnavailable("Legacy guard definition or child-preservation policy differs")
    environment = document.get("EnvironmentVariables")
    if not isinstance(environment, dict) or any(
        environment.get(key) != value
        for key, value in {
            "NODE_NAME": settings.node_name,
            "UPGRADE_ENVIRONMENT_ROOT": settings.upgrade_environment_root,
            "UPGRADE_BOOTSTRAP_ROOT": settings.upgrade_bootstrap_root,
            "NODE_STATE_DIR": settings.node_state_dir,
        }.items()
    ):
        raise UpgradeUnavailable("Legacy guard definition belongs to another state scope")
    return hashlib.sha256(data).hexdigest()


def fixed_launchctl_probe(deadline_at: datetime) -> Callable[[str], bytes | None]:
    """Two fixed service domains share at most five seconds, including cleanup."""
    remaining = (deadline_at - datetime.now(UTC)).total_seconds()
    deadline = time.monotonic() + min(5.0, remaining)
    targets = {"system/com.coire.upgrade-guard", f"gui/{os.getuid()}/com.coire.upgrade-guard"}

    def probe(target: str) -> bytes | None:
        if target not in targets:
            raise UpgradeUnavailable("Recovery service target is not fixed to this owner")
        result = capture_owned_command(
            ["/bin/launchctl", "print", target], timeout=deadline - time.monotonic()
        )
        if result.returncode == 0:
            return result.stdout
        if (
            result.returncode == 113
            and b'Could not find service "com.coire.upgrade-guard"' in result.stderr
        ):
            return None
        raise UpgradeUnavailable("Recovery service observation is unavailable")

    return probe


def observe_legacy_guard_service(
    guard: NativeGuardStatus,
    owner_uid: int,
    probe: Callable[[str], bytes | None],
) -> UpgradeExpiredRecoveryLegacyService:
    """Trusted local read adapter only; never accept caller-supplied OS output.

    Adapter must use fixed launchctl print argv, bounded observation budget/output,
    and return None only for confirmed unloaded services; other errors must raise.
    Missing services alone never identify an unmanaged owner. Raw output can contain
    secrets: retain only its digest and matched process, never log/persist output.
    Physical UID/PID birth/runtime and freshness checks remain mandatory separately.
    """
    if owner_uid < 0:
        raise UpgradeUnavailable("Legacy service owner UID differs")
    matches = []
    for target in ("system/com.coire.upgrade-guard", f"gui/{owner_uid}/com.coire.upgrade-guard"):
        data = probe(target)
        if data is None:
            continue
        if len(data) > 512 * 1024:
            raise UpgradeUnavailable("Legacy service observation exceeds bound")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise UpgradeUnavailable("Legacy service observation is unreadable") from None
        pids = re.findall(r"^\s*pid = ([1-9][0-9]*)\s*$", text, re.MULTILINE)
        states = re.findall(r"^\s*state = ([a-z]+)\s*$", text, re.MULTILINE)
        if len(pids) != 1 or states != ["running"]:
            raise UpgradeUnavailable("Legacy service observation is incomplete")
        if int(pids[0]) == guard.process.pid:
            properties = re.findall(r"^\s*properties = (.+)$", text, re.MULTILINE)
            if len(properties) != 1 or "abandon process group" not in {
                flag.strip() for flag in properties[0].split("|")
            }:
                raise UpgradeUnavailable("Live legacy service does not preserve descendant groups")
            matches.append(
                UpgradeExpiredRecoveryLegacyService(
                    target=target,
                    owner_uid=owner_uid,
                    abandon_process_group=True,
                    process=guard.process,
                    observation_sha256=hashlib.sha256(data).hexdigest(),
                    observed_at=datetime.now(UTC),
                )
            )
    if len(matches) != 1:
        raise UpgradeUnavailable("Legacy guard has no unique positively observed managed owner")
    return matches[0]
