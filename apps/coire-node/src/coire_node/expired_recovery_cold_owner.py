"""Explicit cold ownership; no Source selection, spawn or admission release."""

import hashlib
import os
import pwd
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdSourceIntent,
    UpgradeExpiredRecoveryColdSourceLaunch,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryIntent,
    UpgradeRecoveryRetiredSupervisor,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    PreparedNativeRuntime,
    UpgradeMaintenance,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_definition import read_definition_file
from coire_node.expired_recovery_launchd import fixed_launchctl_probe, verify_legacy_definition
from coire_node.expired_recovery_support import verify_passive_support_identity
from coire_node.recovery_identity import recovery_kernel_boot_identity, verify_retired_identity
from coire_node.recovery_lease import bind_recovery_lease, require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport, _owned_node_process
from coire_node.upgrade_inventory import native_process_entries, read_owner_json
from coire_node.upgrade_runtime import verify_prepared_runtime


def retained_cold_preimages(
    settings: Settings, intent: UpgradeExpiredRecoveryIntent
) -> tuple[NativeUpgradeClaim, NativeRecoveryIntent, NativeGuardStatus, NativeNodeProcess]:
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    operations = PrivateUpgradeStore(bootstrap.path("operations"), create=False)
    operation = PrivateUpgradeStore(operations.path(intent.request.original_job_id), create=False)
    guard = PrivateUpgradeStore(operation.path("guard"), create=False)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    claim = bootstrap.read("claim.json", NativeUpgradeClaim)
    native = guard.read("intent.json", NativeRecoveryIntent)
    status = bootstrap.read("status.json", NativeGuardStatus)
    last = node.read("node-process.json", NativeNodeProcess)
    if claim is None or native is None or status is None or last is None:
        raise UpgradeUnavailable("Cold retained owner preimages are incomplete")
    return claim, native, status, last


def observe_fixture_supervisor(
    settings: Settings,
    previous: NativeGuardStatus,
    last: NativeNodeProcess,
    original_claim: NativeUpgradeClaim | None = None,
) -> UpgradeRecoveryRetiredSupervisor:
    """Positive fixed private fixture provenance; missing service is insufficient."""
    base = Path(settings.upgrade_bootstrap_root).parent
    if (
        Path(settings.node_state_dir) != base / "state"
        or Path(settings.upgrade_environment_root) != base / "envs"
    ):
        raise UpgradeUnavailable("Cold fixture roots do not share the retained scope")
    records = PrivateUpgradeStore(base / "fixture-records", create=False)
    from coire_core.models.upgrade_native_terminal import (
        UpgradeNativeFixtureRetirement,
        UpgradeNativeTerminalSupervisorReceipt,
    )

    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    current_claim = bootstrap.read("claim.json", NativeUpgradeClaim)
    if original_claim is not None and (
        current_claim is None
        or current_claim.state not in {"active", "released"}
        or current_claim.model_dump(exclude={"state", "updated_at"})
        != original_claim.model_dump(exclude={"state", "updated_at"})
    ):
        raise UpgradeConflict("Retired fixture historical claim differs from current scope")
    if current_claim is not None:
        recorded = records.read(
            "retired-adopted-" + current_claim.job_id + ".json", UpgradeNativeFixtureRetirement
        )
        if recorded is not None:
            if (
                recorded.retired_at is None
                or recorded.claim != (original_claim or current_claim)
                or recorded.guard.model_dump(exclude={"observed_at"})
                != previous.model_dump(exclude={"observed_at"})
                or recorded.node_process != last
            ):
                raise UpgradeConflict("Retired adopted fixture current children differ")
            old = recorded.adoption.intent.grant.resolution
            owner = old.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
            origin = PrivateUpgradeStore(
                bootstrap.root / "expired-recovery" / owner.intent.recovery_id, create=False
            )
            if (
                origin.read(
                    "native-terminal.supervisor.receipt.json",
                    UpgradeNativeTerminalSupervisorReceipt,
                )
                != recorded.adoption
            ):
                raise UpgradeConflict("Retired adopted fixture canonical receipt differs")
            script = read_definition_file(base / "fixture-supervisor.py", os.getuid())
            if hashlib.sha256(script).hexdigest() != recorded.definition_sha256:
                raise UpgradeConflict("Retired adopted fixture original definition changed")
            verify_prepared_runtime(
                base / "envs", base / "runtimes", recorded.adoption.intent.runtime
            )
            # Missing processes grant nothing. All three exact PID/birth records
            # must positively retire before the existing cold kernel locks and
            # authenticated owner claim can authorize any recovery effect.
            for identity in (
                recorded.adoption.process,
                recorded.guard.process,
                recorded.node_process.process,
            ):
                verify_retired_identity(identity)
            return UpgradeRecoveryRetiredSupervisor(
                pid=recorded.adoption.process.pid,
                create_time_s=recorded.adoption.process.create_time_s,
                owner_uid=os.getuid(),
                script_sha256=recorded.definition_sha256,
            )
    parent = read_owner_json(records.path("fixture-supervisor.json"))
    children = read_owner_json(records.path("fixture-processes.json"))
    if (
        not isinstance(parent, dict)
        or set(parent) != {"pid", "create_time"}
        or not isinstance(children, dict)
        or set(children) != {"guard", "node"}
    ):
        raise UpgradeUnavailable("Cold fixture supervisor provenance is incomplete")
    for role, identity in (("guard", previous.process), ("node", last.process)):
        record = children[role]
        if (
            not isinstance(record, dict)
            or set(record) != {"pid", "create_time"}
            or (record["pid"], record["create_time"]) != (identity.pid, identity.create_time_s)
        ):
            if role != "node" or current_claim is None:
                raise UpgradeConflict("Cold fixture child provenance differs")
            from coire_core.models.upgrade_native_terminal import UpgradeNativeFixtureUnstartedChild

            retained = records.read(
                "retired-unstarted-" + current_claim.job_id + ".json",
                UpgradeNativeFixtureUnstartedChild,
            )
            if (
                retained is None
                or retained.claim != (original_claim or current_claim)
                or retained.original_node != last
                or not isinstance(record, dict)
                or set(record) != {"pid", "create_time"}
                or (record["pid"], record["create_time"])
                != (retained.candidate_process.pid, retained.candidate_process.create_time_s)
                or (parent["pid"], parent["create_time"])
                != (retained.supervisor.pid, retained.supervisor.create_time_s)
                or retained.native.source_process != last.process
                or (retained.native.job_id, retained.native.generation, retained.native.plan_sha256)
                != (current_claim.job_id, current_claim.generation, current_claim.plan_sha256)
                or hashlib.sha256(
                    read_definition_file(base / "fixture-supervisor.py", os.getuid())
                ).hexdigest()
                != retained.definition_sha256
            ):
                raise UpgradeConflict("Cold unpublished fixture child provenance differs")
            for retired in (retained.supervisor, retained.candidate_process, last.process):
                verify_retired_identity(retired)
    script = read_definition_file(base / "fixture-supervisor.py", os.getuid())
    return UpgradeRecoveryRetiredSupervisor.model_validate(
        {
            "pid": parent["pid"],
            "create_time_s": parent["create_time"],
            "owner_uid": os.getuid(),
            "script_sha256": hashlib.sha256(script).hexdigest(),
        },
        strict=True,
    )


def supervision_lock(
    settings: Settings, intent: UpgradeExpiredRecoveryIntent
) -> AbstractContextManager[int | None]:
    """Retain the fixture's actual restart exclusion alongside native ownership."""
    if intent.request.supervision == "fixture":
        records = PrivateUpgradeStore(
            Path(settings.upgrade_bootstrap_root).parent / "fixture-records", create=False
        )
        return records.ownership("fixture-supervisor.lock")
    return nullcontext()


def observe_managed_definition(settings: Settings, previous: NativeGuardStatus) -> str:
    """Require one fixed installed definition; absent services alone are insufficient."""
    try:
        owner = pwd.getpwuid(os.getuid())
    except KeyError:
        raise UpgradeUnavailable("Cold managed owner user is unknown") from None
    definitions = []
    for path, uid in (
        (Path("/Library/LaunchDaemons/com.coire.upgrade-guard.plist"), 0),
        (Path(owner.pw_dir) / "Library/LaunchAgents/com.coire.upgrade-guard.plist", os.getuid()),
    ):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise UpgradeUnavailable("Cold managed definition cannot be observed") from None
        definitions.append(
            verify_legacy_definition(
                settings, previous, read_definition_file(path, uid), owner.pw_name
            )
        )
    if len(definitions) != 1:
        raise UpgradeUnavailable("Cold managed definition is absent or ambiguous")
    return definitions[0]


def verify_cold_physical(
    settings: Settings,
    claim: UpgradeExpiredRecoveryColdClaim,
    projection: UpgradeExpiredRecoveryColdSourceIntent | None = None,
) -> None:
    """Retirement plus actual scope/exclusion facts, never absence-based authority."""
    intent = claim.intent
    if verify_passive_support_identity(settings, intent, claim.runtime) != claim.process:
        raise UpgradeUnavailable("Cold current support identity differs")
    if recovery_kernel_boot_identity(intent.deadline_at) != claim.kernel_boot_identity:
        raise UpgradeUnavailable("Cold kernel changed; separate crash succession is required")
    envs = Path(settings.upgrade_environment_root)
    source_python = verify_prepared_runtime(
        envs, envs.parent / "runtimes", claim.original_native.source.runtime
    )
    if claim.previous_guard.bootstrap_runtime != claim.original_native.source.runtime:
        verify_prepared_runtime(
            envs, envs.parent / "runtimes", claim.previous_guard.bootstrap_runtime
        )
    retired = [claim.previous_guard.process, claim.original_native.source_process]
    if claim.continuation is None:
        retired.append(claim.last_node.process)
    for identity in retired:
        verify_retired_identity(identity)
    probe = fixed_launchctl_probe(intent.deadline_at)
    for target in ("system/com.coire.upgrade-guard", f"gui/{os.getuid()}/com.coire.upgrade-guard"):
        if probe(target) is not None:
            raise UpgradeUnavailable("Cold legacy service is still loaded")
    if claim.supervision == "fixture":
        from coire_node.expired_recovery_cold_continuation import fixture_origin

        origin = fixture_origin(settings, claim)
        supervisor = observe_fixture_supervisor(
            settings, origin.previous_guard, origin.last_node, origin.original_claim
        )
        if supervisor != claim.retired_supervisor:
            raise UpgradeConflict("Cold fixture supervisor preimage changed")
        verify_retired_identity(supervisor)
    else:
        # No inference of unmanaged ownership from absent launchd services.
        # A positively bound installed definition is mandatory in managed mode.
        if (
            observe_managed_definition(settings, claim.previous_guard)
            != claim.managed_definition_sha256
        ):
            raise UpgradeConflict("Cold managed definition preimage changed")
    processes = native_process_entries("cold-recovery", (str(envs.parent),))
    allowed = {(claim.process.pid, claim.process.create_time_s)}
    if claim.continuation is not None:
        from coire_node.expired_recovery_cold_continuation import previous_source

        _previous, retained = previous_source(settings, claim.continuation)
        child = _owned_node_process(retained.process, source_python)
        if child is None or child.cmdline() != [str(source_python), "-I", "-B", "-m", "coire_node"]:
            raise UpgradeUnavailable("Cold continuation retained Source identity differs")
        allowed.add((retained.process.pid, retained.process.create_time_s))
    source_identities: list[NodeAgentProcessIdentity] = []
    if projection is not None:
        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
        for attempt in range(3):
            launch = run.read(
                f"cold.source.launch.{attempt}.json", UpgradeExpiredRecoveryColdSourceLaunch
            )
            receipt = run.read(
                f"cold.source.process.{attempt}.json", UpgradeExpiredRecoveryColdSourceProcess
            )
            if launch is None:
                if receipt is not None:
                    raise UpgradeConflict("Cold Source process has no retained launch")
                continue
            if launch.source.projection != projection or launch.attempt != attempt:
                raise UpgradeConflict("Cold Source launch provenance differs")
            if receipt is not None:
                if receipt.launch != launch:
                    raise UpgradeConflict("Cold Source process receipt differs")
                source_identities.append(receipt.process)
            matches = []
            for entry in processes:
                if (entry.process_id, entry.process_created_at) in allowed:
                    continue
                if entry.process_id is None or entry.process_created_at is None:
                    raise UpgradeUnavailable("Cold Source launch inventory is uncertain")
                if (
                    not launch.started_at.timestamp()
                    <= entry.process_created_at
                    < intent.deadline_at.timestamp()
                ):
                    continue
                identity = NodeAgentProcessIdentity(
                    pid=entry.process_id,
                    create_time_s=entry.process_created_at,
                    environment_sha256=projection.source_selection.runtime.environment_sha256,
                )
                # A pending launch permits only its unique positively observed
                # fixed Source module, never arbitrary native process adoption.
                child = _owned_node_process(identity, source_python)
                if child is not None:
                    if child.cmdline() != [str(source_python), "-I", "-B", "-m", "coire_node"]:
                        raise UpgradeUnavailable(
                            "Cold Source argv does not select the sealed Source"
                        )
                    if receipt is not None and receipt.process != identity:
                        raise UpgradeConflict("Cold Source launch has a foreign live identity")
                    matches.append(identity)
            if len(matches) > 1:
                raise UpgradeUnavailable("Cold Source launch has multiple physical owners")
            for identity in matches:
                allowed.add((identity.pid, identity.create_time_s))
                source_identities.append(identity)
    if any((entry.process_id, entry.process_created_at) not in allowed for entry in processes):
        raise UpgradeUnavailable("Cold private native scope still has physical owners")
    current_claim, current_native, current_guard, current_node = retained_cold_preimages(
        settings, intent
    )
    expected_guard = current_guard == claim.previous_guard
    if claim.continuation is not None and claim.continuation.post_handback is not None:
        from coire_core.models.upgrade_recovery_continuation import (
            UpgradeColdSourceKeeperRetirement,
        )

        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        run = PrivateUpgradeStore(
            bootstrap.path("expired-recovery") / intent.recovery_id, create=False
        )
        retirement = run.read(
            "source.keeper.retirement.receipt.json", UpgradeColdSourceKeeperRetirement
        )
        if (
            retirement is None
            or retirement.retired_at is None
            or retirement.grant.binding != claim.continuation
        ):
            raise UpgradeUnavailable(
                "Post-handback Source writer has no positive retirement receipt"
            )
        expected_guard = expected_guard or (
            current_guard.process == claim.previous_guard.process
            and current_guard.selected == claim.previous_guard.selected
            and current_guard.bootstrap_runtime == claim.previous_guard.bootstrap_runtime
        )
    if projection is not None:
        if projection.owner != claim:
            raise UpgradeConflict("Cold Source projection belongs to another owner")
        expected_guard = expected_guard or (
            current_guard.node == claim.previous_guard.node
            and current_guard.process == claim.process
            and current_guard.bootstrap_runtime == claim.runtime
            and current_guard.selected == projection.source_selection
        )
    if (
        current_claim != claim.original_claim
        or current_native
        not in (
            (claim.original_native,)
            + ((claim.continuation.projected_native,) if claim.continuation is not None else ())
            + ((projection.projected_native,) if projection is not None else ())
        )
        or not expected_guard
        or not (
            current_node == claim.last_node
            or (
                projection is not None
                and current_node.selected == projection.source_selection
                and current_node.process in source_identities
            )
        )
    ):
        raise UpgradeConflict("Cold retained authority changed during physical observation")
    if datetime.now(UTC) >= intent.deadline_at:
        raise UpgradeUnavailable("Cold physical proof exceeded captured recovery budget")


@contextmanager
def initial_cold_owner(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
) -> Iterator[UpgradeExpiredRecoveryColdClaim]:
    """No exception-based resume; retain exact claim before audited acceptance.

    Both original native writer and recovery locks stay held for caller lifetime.
    Lost Core acknowledgement preserves local claim and requires explicit probe/
    exact-identity resume; it never recaptures a claim time or recovery epoch.
    """
    if intent.request.recovery_mode != "cold":
        raise UpgradeConflict("Preservation intent cannot select cold ownership")
    if control.verify_support_context(intent) != intent:
        raise UpgradeUnavailable("Cold support context differs")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    grant = control.read_cold_recovery_continuation(intent)
    if grant is not None and grant.unstarted_startup is not None:
        from coire_node.expired_recovery_unstarted_lineage import observe_unstarted_custodian

        late = grant.binding.post_handback
        if (
            late is not None
            and late.stage == "keeper_retired"
            and late.keeper_custodian_recovery_id == grant.unstarted_startup.intent.recovery_id
        ):
            from coire_node.expired_recovery_unstarted_lineage import (
                observe_retired_keeper_custodian,
            )

            observed_startup, retirement = observe_retired_keeper_custodian(
                settings, grant.unstarted_startup.intent
            )
            if retirement.sha256() != late.keeper_retirement_sha256:
                raise UpgradeConflict("Cold keeper custody retirement digest differs")
        else:
            observed_startup = observe_unstarted_custodian(settings, grant.unstarted_startup.intent)
        if observed_startup != grant.unstarted_startup:
            raise UpgradeConflict("Cold unstarted custody retirement changed before ownership")
    with (
        supervision_lock(settings, intent) as supervision_descriptor,
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        ExitStack() as stack,
    ):
        if grant is not None and grant.binding.post_handback is not None:
            from coire_node.expired_recovery_source_keeper_retirement import retire_source_keeper

            retire_source_keeper(settings, grant, control, runtime)
        lifetime_descriptor = stack.enter_context(bootstrap.ownership("lifetime.lock"))
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
        if run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim) is not None:
            raise UpgradeUnavailable("Retained cold claim requires exact explicit resume")
        if grant is not None and grant.binding.post_handback is not None:
            from coire_core.models.upgrades import NodeMaintenanceCommand
            from coire_node.maintenance import NodeMaintenance

            fence = intent.local_recovery_maintenance
            if fence is None:
                raise UpgradeUnavailable(
                    "Released Source requires a distinct captured recovery fence"
                )
            gate = NodeMaintenance(
                Path(settings.node_state_dir) / "upgrades", grant.binding.original_claim.node
            )
            before_fence = grant.binding.post_handback.released_maintenance
            current_fence = gate.current()
            if current_fence not in (before_fence, fence):
                raise UpgradeConflict("Recovery fence installation has another maintenance owner")
            saved = run.read("local.previous.released.maintenance.json", UpgradeMaintenance)
            if saved is None:
                run.write("local.previous.released.maintenance.json", before_fence, replace=False)
            elif saved != before_fence:
                raise UpgradeConflict("Recovery fence changes original released history")
            command = NodeMaintenanceCommand(maintenance=fence, action="apply")
            previous_command = run.read("local.recovery.fence.intent.json", NodeMaintenanceCommand)
            if previous_command is None:
                run.write("local.recovery.fence.intent.json", command, replace=False)
            elif previous_command != command:
                raise UpgradeConflict("Recovery fence installation changes its captured intent")
            if control.verify_support_context(intent) != intent:
                raise UpgradeUnavailable("Recovery fence installation lost Core authority")
            ack = gate.apply(
                command,
                recovery_preimage=before_fence if before_fence.state != "released" else None,
            )
            run.write("local.recovery.fence.receipt.json", ack)
        original, native, status, last = retained_cold_preimages(settings, intent)
        previous = None
        if grant is not None:
            from coire_node.expired_recovery_cold_continuation import previous_source

            previous, _source = previous_source(settings, grant.binding)
            expected_claim = (
                grant.binding.post_handback.released_claim
                if grant.binding.post_handback is not None
                else grant.binding.original_claim
            )
            if (original, native, last) != (
                expected_claim,
                grant.binding.projected_native,
                grant.binding.retained_node,
            ):
                raise UpgradeConflict("Cold continuation startup preimages differ")
            if grant.binding.post_handback is None and status != grant.binding.previous_guard:
                raise UpgradeConflict("Cold continuation startup guard preimage differs")
            if grant.binding.post_handback is not None:
                if (status.process, status.selected, status.bootstrap_runtime) != (
                    grant.binding.previous_guard.process,
                    grant.binding.previous_guard.selected,
                    grant.binding.previous_guard.bootstrap_runtime,
                ):
                    raise UpgradeConflict("Retired Source guard final metadata differs")
                status = grant.binding.previous_guard
            native = grant.binding.original_native
        process = verify_passive_support_identity(settings, intent, runtime)
        supervisor = (
            previous.retired_supervisor
            if previous is not None
            else observe_fixture_supervisor(settings, status, last)
            if intent.request.supervision == "fixture"
            else None
        )
        claim = UpgradeExpiredRecoveryColdClaim(
            intent=intent,
            process=process,
            runtime=runtime,
            kernel_boot_identity=recovery_kernel_boot_identity(intent.deadline_at),
            original_claim=original,
            original_native=native,
            previous_guard=status,
            last_node=last,
            supervision=intent.request.supervision,
            retired_supervisor=supervisor,
            managed_definition_sha256=observe_managed_definition(settings, status)
            if intent.request.supervision == "launchd"
            else None,
            claimed_at=datetime.now(UTC),
            continuation=grant.binding if grant is not None else None,
        )
        projection = run.read("cold.source.intent.json", UpgradeExpiredRecoveryColdSourceIntent)
        verify_cold_physical(settings, claim, projection)
        if control.verify_support_context(intent) != intent:
            raise UpgradeUnavailable("Cold authority changed before claim retention")
        verify_cold_physical(settings, claim, projection)
        # Hashing retained runtime graphs can take seconds. Capture the claim
        # epoch after those checks, without changing any operation deadline.
        claim = UpgradeExpiredRecoveryColdClaim.model_validate(
            claim.model_dump(mode="json") | {"claimed_at": datetime.now(UTC)}
        )
        run.write("cold.claim.json", claim, replace=False)
        if (
            control.accept_cold_recovery_owner(claim) != claim
            or control.verify_cold_recovery_owner(claim) != claim
        ):
            raise UpgradeUnavailable("Cold owner acknowledgement differs")
        verify_cold_physical(settings, claim)
        run.write("cold.activation.json", claim, replace=False)
        with bind_recovery_lease(
            bootstrap,
            claim.sha256(),
            descriptor,
            lifetime_descriptor=lifetime_descriptor,
            supervision_descriptor=supervision_descriptor,
        ):
            require_recovery_lease(bootstrap, claim.sha256())
            yield claim


@contextmanager
def resumed_cold_owner(
    settings: Settings,
    claim: UpgradeExpiredRecoveryColdClaim,
    control: NativeGuardTransport,
) -> Iterator[UpgradeExpiredRecoveryColdClaim]:
    """Exact-identity probe/replay, never initial fallback or recaptured epochs."""
    claim = UpgradeExpiredRecoveryColdClaim.model_validate_json(claim.model_dump_json())
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with (
        supervision_lock(settings, claim.intent) as supervision_descriptor,
        bootstrap.ownership("lifetime.lock") as lifetime_descriptor,
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
    ):
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(claim.intent.recovery_id), create=False)
        if run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim) != claim:
            raise UpgradeConflict("Retained cold owner differs")
        state = control.probe_cold_recovery_owner(claim)
        if state.owner is not None and state.owner != claim:
            raise UpgradeConflict("Cold accepted owner differs")
        projection = run.read("cold.source.intent.json", UpgradeExpiredRecoveryColdSourceIntent)
        verify_cold_physical(settings, claim, projection)
        if state.owner is None:
            if datetime.now(UTC) >= min(
                claim.intent.deadline_at, claim.claimed_at + timedelta(seconds=30)
            ):
                raise UpgradeUnavailable("Cold initial claim window expired")
            if control.accept_cold_recovery_owner(claim) != claim:
                raise UpgradeUnavailable("Cold initial replay acknowledgement differs")
        if control.verify_cold_recovery_owner(claim) != claim:
            raise UpgradeUnavailable("Cold exact owner authority changed")
        verify_cold_physical(settings, claim, projection)
        previous = run.read("cold.activation.json", UpgradeExpiredRecoveryColdClaim)
        if previous is not None and previous != claim:
            raise UpgradeConflict("Cold activation history differs")
        if previous is None:
            run.write("cold.activation.json", claim, replace=False)
        with bind_recovery_lease(
            bootstrap,
            claim.sha256(),
            descriptor,
            lifetime_descriptor=lifetime_descriptor,
            supervision_descriptor=supervision_descriptor,
        ):
            require_recovery_lease(bootstrap, claim.sha256())
            yield claim
