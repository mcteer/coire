"""Prepare independent native recovery metadata before enabling managed startup."""

import argparse
import os
import plistlib
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    NativeBootstrapRefresh,
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRuntimeSelection,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    PreparedNativeRuntime,
    UpgradeEvidence,
    UpgradeKind,
    UpgradeReleaseRegister,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.upgrade_bootstrap")


@tracer.start_as_current_span(
    "coire.node.upgrade_bootstrap_prepare", record_exception=False, set_status_on_exception=False
)
def prepare_guard_bootstrap(
    settings: Settings, request: UpgradeReleaseRegister
) -> NativeRuntimeSelection:
    """Seed verified source receipts without selecting a runtime or starting any process.

    Interrupted preparation is not ready. An exact retry may complete it, while
    conflicting metadata or an existing recovery owner cannot be overwritten.
    """
    if settings.node_name not in {"coire-edge-a", "coire-edge-b"}:
        raise UpgradeUnavailable("Native bootstrap requires an intended Studio")
    request = UpgradeReleaseRegister.model_validate(request.model_dump(mode="json"))
    release = verify_prepared_release(Path(settings.upgrade_prepared_dir), request)
    if release.manifest.kind is not UpgradeKind.STUDIO:
        raise UpgradeConflict("Native bootstrap source is not a Studio release")
    envs = Path(settings.upgrade_environment_root)
    environment_id = release.manifest.environment_id
    if environment_id is None:
        raise UpgradeUnavailable("Native bootstrap source has no retained environment")
    runtime = PrivateUpgradeStore(envs / ".identities").read(
        environment_id + ".json", PreparedNativeRuntime
    )
    if runtime is None or runtime.environment_sha256 != release.manifest.environment_sha256:
        raise UpgradeUnavailable("Native bootstrap source environment differs")
    verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    pointer = envs / "current"
    if not pointer.is_symlink() or pointer.resolve(strict=True) != (envs / environment_id).resolve(
        strict=True
    ):
        raise UpgradeConflict("Native bootstrap source is not physically selected")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    with root.ownership("control.lock"), root.ownership("lifetime.lock"):
        claim = root.read("claim.json", NativeUpgradeClaim)
        if claim is not None:
            return refresh_guard_source(settings, root, runtime, release.manifest_sha256, claim)
        previous = root.read("runtime.json", PreparedNativeRuntime)
        launcher = root.read("launcher-runtime.json", PreparedNativeRuntime)
        if launcher is not None and launcher != runtime:
            raise UpgradeConflict("Independent native launcher runtime differs")
        selection = root.read("selection.json", NativeRuntimeSelection)
        if previous is not None and previous != runtime:
            raise UpgradeConflict("Independent native bootstrap runtime differs")
        if selection is not None and (
            selection.node,
            selection.runtime,
            selection.release_sha256,
            selection.job_id,
            selection.generation,
        ) != (settings.node_name, runtime, release.manifest_sha256, None, 0):
            raise UpgradeConflict("Independent native bootstrap source differs")
        if root.path("status.json").exists():
            raise UpgradeConflict("Independent native bootstrap has already been started")
        # Graph verification may have been expensive. The final selection check
        # protects against a concurrent installer changing the active source.
        if pointer.resolve(strict=True) != (envs / environment_id).resolve(strict=True):
            raise UpgradeConflict("Native bootstrap physical source changed")
        if launcher is None:
            root.write("launcher-runtime.json", runtime, replace=False)
        if previous is None:
            root.write("runtime.json", runtime, replace=False)
        if selection is None:
            selection = NativeRuntimeSelection(
                node="coire-edge-a" if settings.node_name == "coire-edge-a" else "coire-edge-b",
                runtime=runtime,
                release_sha256=release.manifest_sha256,
                generation=0,
                selected_at=datetime.now(UTC),
            )
            root.write("selection.json", selection, replace=False)
        return selection


def refresh_guard_source(
    settings: Settings,
    root: PrivateUpgradeStore,
    runtime: PreparedNativeRuntime,
    release_sha256: str,
    claim: NativeUpgradeClaim,
    *,
    refreshing_guard: NodeAgentProcessIdentity | None = None,
) -> NativeRuntimeSelection:
    """Caller holds both kernel locks; history survives a physically idle owner refresh."""
    from coire_node.upgrade_control import NativeUpgradeControl
    from coire_node.upgrade_guard import _owned_native_process, _owned_node_process

    selected = root.read("selection.json", NativeRuntimeSelection)
    previous = root.read("runtime.json", PreparedNativeRuntime)
    if (
        claim.state != "released"
        or selected is None
        or previous is None
        or (
            selected.node,
            selected.job_id,
            selected.generation,
            selected.runtime,
            selected.release_sha256,
        )
        != (settings.node_name, claim.job_id, claim.generation, runtime, release_sha256)
    ):
        raise UpgradeConflict("Native helper refresh requires a released exact current Source")
    journal = NativeUpgradeControl(settings).journal(claim.job_id)
    latest = journal.receipts.latest()
    if (
        latest is None
        or latest.phase not in TERMINAL_UPGRADE_PHASES
        or latest.outcome != "complete"
        or latest.observed_sha256 != release_sha256
    ):
        raise UpgradeUnavailable("Native helper refresh has no positive Source terminal proof")
    evidence = UpgradeEvidence.model_validate_json(latest.evidence)
    if evidence.runtime_sha256 != runtime.environment_sha256 or not evidence.cleanup_verified:
        raise UpgradeUnavailable("Native helper refresh Source cleanup is unverified")
    envs = Path(settings.upgrade_environment_root)
    source = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").read(
        "node-process.json", NativeNodeProcess
    )
    if (
        source is None
        or source.selected != selected
        or _owned_node_process(source.process, envs / runtime.environment_id / "bin/python3")
        is None
    ):
        raise UpgradeUnavailable("Native helper refresh Source process is not physically owned")
    key = f"refresh-{selected.generation}-{runtime.environment_sha256}.json"
    retained = root.read(key, NativeBootstrapRefresh)
    status = root.read("status.json", NativeGuardStatus)
    old_runtime = retained.previous_runtime if retained is not None else previous
    if status is not None:
        if status.bootstrap_runtime not in (old_runtime, runtime):
            raise UpgradeConflict("Native helper refresh prior owner differs")
        owned = _owned_native_process(
            status.process,
            envs / status.bootstrap_runtime.environment_id / "bin/python3",
            "coire_node.upgrade_guard",
        )
        if owned is not None and (
            refreshing_guard is None
            or refreshing_guard != status.process
            or refreshing_guard.pid != os.getpid()
            or owned.create_time() != refreshing_guard.create_time_s
        ):
            raise UpgradeConflict("Native helper refresh prior owner is still running")
    if previous == runtime and retained is None:
        return selected
    if retained is None:
        retained = NativeBootstrapRefresh(
            previous_runtime=previous,
            selected=selected,
            source_process=source.process,
            prepared_at=datetime.now(UTC),
        )
        root.write(key, retained, replace=False)
    elif (
        retained.selected != selected
        or retained.source_process != source.process
        or previous not in (retained.previous_runtime, runtime)
    ):
        raise UpgradeConflict("Native helper refresh replay differs")
    verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    if (envs / "current").resolve(strict=True) != (envs / runtime.environment_id).resolve(
        strict=True
    ):
        raise UpgradeConflict("Native helper refresh physical Source changed")
    if (
        root.read("claim.json", NativeUpgradeClaim) != claim
        or root.read("selection.json", NativeRuntimeSelection) != selected
        or PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").read(
            "node-process.json", NativeNodeProcess
        )
        != source
        or _owned_node_process(source.process, envs / runtime.environment_id / "bin/python3")
        is None
    ):
        raise UpgradeUnavailable("Native helper refresh Source authority changed")
    committed = retained.model_copy(update={"state": "committed"})
    root.write(key, committed)
    root.write("bootstrap-refresh.json", committed)
    # Publish the interpreter only after its replay proof is durable. A crash
    # after this write must not require the previously observed node to survive.
    root.write("runtime.json", runtime)
    return selected


def render_guard_plist(settings: Settings, template: bytes, user: str) -> bytes:
    """Render installation data with an independently retained interpreter selection."""
    if settings.node_name not in {"coire-edge-a", "coire-edge-b"} or not re.fullmatch(
        r"[a-z_][a-z0-9_-]{0,31}", user
    ):
        raise UpgradeUnavailable("Native recovery launchd identity is invalid")
    if len(template) > 64 * 1024:
        raise UpgradeUnavailable("Native recovery launchd template exceeds its bound")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    runtime = root.read("launcher-runtime.json", PreparedNativeRuntime) or root.read(
        "runtime.json", PreparedNativeRuntime
    )
    if runtime is None:
        raise UpgradeUnavailable("Native recovery launchd bootstrap is absent")
    envs = Path(settings.upgrade_environment_root)
    if not envs.is_absolute() or envs.name != "envs":
        raise UpgradeUnavailable("Native recovery launchd prefix differs")
    verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    if Path(settings.upgrade_bootstrap_root) != envs.parent / "upgrade-bootstrap":
        raise UpgradeConflict("Native recovery launchd bootstrap scope differs")
    replacements = {
        "__PREFIX__": str(envs.parent),
        "__BOOTSTRAP_ENVIRONMENT__": runtime.environment_id,
        "__USER__": user,
        "__NODE_NAME__": settings.node_name,
    }
    document = template.decode("utf-8")
    for key, value in replacements.items():
        # Escape installation strings as XML values, including a prefix containing
        # spaces or ampersands. No shell command interpolates these values.
        document = document.replace(key, value.replace("&", "&amp;").replace("<", "&lt;"))
    if re.search(r"__[A-Z_]+__", document):
        raise UpgradeUnavailable("Native recovery launchd template is incomplete")
    data = plistlib.loads(document.encode())
    expected = [
        str(envs / runtime.environment_id / "bin/python3"),
        "-I",
        "-B",
        "-m",
        "coire_node.upgrade_guard",
    ]
    if (
        not isinstance(data, dict)
        or data.get("Label") != "com.coire.upgrade-guard"
        or data.get("ProgramArguments") != expected
        or data.get("UserName") != user
        or data.get("AbandonProcessGroup") is not True
    ):
        raise UpgradeConflict("Native recovery launchd authority differs")
    return plistlib.dumps(data, sort_keys=False)


def render_managed_node_plist(settings: Settings, installed: bytes, user: str) -> bytes:
    """Enable identity checks on the existing node service without changing its command."""
    # Reuse the retained-runtime/platform/identity validation of the guard renderer.
    if len(installed) > 64 * 1024 or settings.node_name not in {"coire-edge-a", "coire-edge-b"}:
        raise UpgradeUnavailable("Managed node launchd installation is invalid")
    prefix = Path(settings.upgrade_environment_root).parent
    if Path(settings.upgrade_bootstrap_root) != prefix / "upgrade-bootstrap":
        raise UpgradeConflict("Managed node launchd bootstrap scope differs")
    runtime = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root)).read(
        "runtime.json", PreparedNativeRuntime
    )
    if runtime is None:
        raise UpgradeUnavailable("Managed node recovery bootstrap is absent")
    verify_prepared_runtime(Path(settings.upgrade_environment_root), prefix / "runtimes", runtime)
    document = plistlib.loads(installed)
    if (
        not isinstance(document, dict)
        or document.get("Label") != "com.coire.node"
        or document.get("UserName") != user
        or document.get("ProgramArguments")
        != [str(prefix / "envs/current/bin/python3"), "-m", "coire_node"]
        or document.get("AbandonProcessGroup") is not True
    ):
        raise UpgradeConflict("Managed node launchd authority differs")
    environment = document.get("EnvironmentVariables")
    if not isinstance(environment, dict) or environment.get("NODE_NAME") != settings.node_name:
        raise UpgradeConflict("Managed node launchd target differs")
    environment.update(
        UPGRADES_ENABLED="1",
        UPGRADE_ENVIRONMENT_ROOT=str(prefix / "envs"),
        UPGRADE_BOOTSTRAP_ROOT=str(prefix / "upgrade-bootstrap"),
        UPGRADE_PREPARED_DIR=settings.upgrade_prepared_dir,
    )
    return plistlib.dumps(document, sort_keys=False)


def main() -> None:
    """Read one bounded operator staging request; never execute engines or launchctl."""
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--render-guard-plist", type=Path)
        parser.add_argument("--render-node-plist", type=Path)
        parser.add_argument("--user")
        args = parser.parse_args()
        if args.render_guard_plist is not None and args.render_node_plist is not None:
            raise UpgradeUnavailable("Select one launchd render operation")
        if args.render_node_plist is not None:
            if not args.user:
                raise UpgradeUnavailable("Managed node launchd user is required")
            with args.render_node_plist.open("rb") as stream:
                template = stream.read(64 * 1024 + 1)
            sys.stdout.buffer.write(render_managed_node_plist(Settings(), template, args.user))
            sys.stdout.buffer.flush()
            return
        if args.render_guard_plist is not None:
            if not args.user:
                raise UpgradeUnavailable("Native recovery launchd user is required")
            with args.render_guard_plist.open("rb") as stream:
                template = stream.read(64 * 1024 + 1)
            sys.stdout.buffer.write(render_guard_plist(Settings(), template, args.user))
            sys.stdout.buffer.flush()
            return
        if args.user is not None:
            raise UpgradeUnavailable("Launchd user requires a render operation")
        document = sys.stdin.buffer.read(1025)
        if len(document) > 1024:
            raise UpgradeUnavailable("Native bootstrap request exceeds its bound")
        selected = prepare_guard_bootstrap(
            Settings(), UpgradeReleaseRegister.model_validate_json(document)
        )
        sys.stdout.write(selected.model_dump_json() + "\n")
        sys.stdout.flush()
    except (UpgradeConflict, UpgradeUnavailable, OSError, ValueError):
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
