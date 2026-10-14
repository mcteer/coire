"""Upgrade administration and bounded reconnect using the shared API SDK."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from uuid import UUID

from pydantic import BaseModel, SecretStr, TypeAdapter, ValidationError

from coire_core.client import UpgradeClient
from coire_core.errors import CoireError, UpgradeUnavailable
from coire_core.models.upgrade_native_terminal import UpgradeNativeTerminalRequest
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancelRequest,
    UpgradeExpiredRecoveryRequest,
    UpgradeExpiredRecoveryUnstartedCloseRequest,
    UpgradeRecoveryHostHoldRequest,
    UpgradeRecoverySchemaRequest,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationRead,
    UpgradeColdContinuationRequest,
    UpgradeColdContinuationSchemaRequest,
)
from coire_core.models.upgrade_unclaimed import UpgradeUnclaimedCancellationRequest
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    CoreHandoverStartRequest,
    UpgradeCancelRequest,
    UpgradeId,
    UpgradeKind,
    UpgradeOperation,
    UpgradePhase,
    UpgradeRollbackRequest,
    UpgradeStartRequest,
)


def job_id(value: str) -> str:
    try:
        return TypeAdapter(UpgradeId).validate_python(value)
    except ValidationError:
        raise argparse.ArgumentTypeError("Expected an upgrade job ULID") from None


def timeout_seconds(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a timeout in seconds") from None
    if not 0 < result <= 86400:
        raise argparse.ArgumentTypeError("Timeout must be between zero and 86400 seconds")
    return result


def parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    upgrade = commands.add_parser("upgrades", help="operate managed upgrades through the admin API")
    verbs = upgrade.add_subparsers(dest="upgrade_command", required=True)
    releases = verbs.add_parser("releases")
    releases.add_argument("--kind", choices=[item.value for item in UpgradeKind])
    releases.add_argument("--cursor", type=UUID)
    versions = verbs.add_parser("versions", help="show observed versions, readiness and blockers")
    node = verbs.add_parser("node")
    node.add_argument("node", choices=("coire-edge-a", "coire-edge-b"))
    core = verbs.add_parser("control-plane")
    handover = verbs.add_parser("executor-handover")
    handover.add_argument("--release", type=UUID, required=True)
    handover.add_argument("--expected-incumbent-seed", required=True)
    handover.add_argument("--idempotency-key", type=UUID, required=True)
    handover_status = verbs.add_parser("executor-status")
    handover_status.add_argument("handover_id", type=UUID)
    rollback = verbs.add_parser("rollback")
    rollback.add_argument("job_id", type=job_id)
    for parser in (node, core, rollback):
        parser.add_argument("--expected-source", required=True, help="exact observed Source digest")
        parser.add_argument(
            "--idempotency-key", type=UUID, required=True, help="reuse this UUID on request retry"
        )
        parser.add_argument("--acknowledge-maintenance", action="store_true", required=True)
    for parser in (node, core):
        parser.add_argument("--release", type=UUID, required=True)
    status = verbs.add_parser("status")
    status.add_argument("job_id", type=job_id)
    status.add_argument("--watch", action="store_true")
    status.add_argument("--timeout", type=timeout_seconds, default=300.0)
    cancel = verbs.add_parser("cancel")
    cancel.add_argument("job_id", type=job_id)
    cancel.add_argument("--reason", default="administrator_cancelled")
    recovery = verbs.add_parser(
        "expired-recovery", help="accept one explicit intent from bounded JSON stdin"
    )
    recovery_status = verbs.add_parser("recovery-status")
    recovery_status.add_argument("recovery_id", type=job_id)
    recovery_cancel = verbs.add_parser("recovery-cancel")
    recovery_cancel.add_argument("recovery_id", type=job_id)
    recovery_cancel.add_argument("--intent-sha", required=True)
    recovery_close = verbs.add_parser(
        "recovery-close-unstarted", help="close an expired unstarted attempt using JSON stdin"
    )
    recovery_close.add_argument("recovery_id", type=job_id)
    unclaimed = verbs.add_parser(
        "unclaimed-cancel",
        help="request exact unpublished preparation cancellation using JSON stdin",
    )
    unclaimed.add_argument("job_id", type=job_id)
    native_terminal = verbs.add_parser(
        "recovery-native-terminal",
        help="acknowledge canonical resolution in the legacy native reader using JSON stdin",
    )
    native_terminal.add_argument("recovery_id", type=job_id)
    host_hold = verbs.add_parser(
        "recovery-host-hold", help="reconcile an exact terminal recovery host hold using JSON stdin"
    )
    host_hold.add_argument("recovery_id", type=job_id)
    schema = verbs.add_parser("recovery-schema")
    schema.add_argument("job_id", type=job_id)
    schema_prepare = verbs.add_parser(
        "recovery-schema-prepare", help="prepare the known legacy schema using JSON stdin"
    )
    continuation = verbs.add_parser(
        "recovery-continue-cold",
        help="accept one explicitly approved cold continuation using JSON stdin",
    )
    cold_schema = verbs.add_parser(
        "recovery-cold-schema", help="inspect a cold predecessor schema using JSON stdin"
    )
    cold_schema.add_argument("recovery_id", type=job_id)
    cold_schema_prepare = verbs.add_parser(
        "recovery-cold-schema-prepare",
        help="prepare exact separate recovery supersession constraints using JSON stdin",
    )
    for parser in (
        releases,
        versions,
        node,
        core,
        rollback,
        status,
        cancel,
        handover,
        handover_status,
        recovery,
        recovery_status,
        recovery_cancel,
        recovery_close,
        host_hold,
        native_terminal,
        unclaimed,
        schema,
        schema_prepare,
        continuation,
        cold_schema,
        cold_schema_prepare,
    ):
        parser.add_argument("--json", action="store_true", dest="json_output")


def emit(value: BaseModel, *, json_output: bool) -> None:
    if isinstance(value, UpgradeOperation) and not json_output:
        print(f"{value.job_id} {value.phase.value}")
    else:
        print(value.model_dump_json(indent=None if json_output else 2))


def input_model[T: BaseModel](model: type[T]) -> T:
    data = sys.stdin.buffer.read(512 * 1024 + 1)
    if len(data) > 512 * 1024:
        raise UpgradeUnavailable("Upgrade JSON input exceeds its fixed bound")
    try:
        return model.model_validate_json(data)
    except ValidationError:
        raise UpgradeUnavailable("Upgrade JSON input does not match its strict contract") from None


async def watch(client: UpgradeClient, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    cursor, previous = 0, None
    while time.monotonic() < deadline:
        try:
            remaining = deadline - time.monotonic()
            async with asyncio.timeout(remaining):
                page = await client.events(args.job_id, after=cursor)
        except (UpgradeUnavailable, TimeoutError):
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(1, remaining))
            continue
        operation = page.operation
        if operation.job_id != args.job_id:
            raise ValueError("Upgrade watch received another job")
        identity = (operation.phase, operation.receipt_sequence, operation.updated_at)
        if identity != previous:
            emit(operation, json_output=args.json_output)
            previous = identity
        cursor = page.cursor
        if operation.phase in TERMINAL_UPGRADE_PHASES:
            return (
                0
                if operation.phase in {UpgradePhase.SUCCEEDED, UpgradePhase.CANCELLED_UNCHANGED}
                else 1
            )
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(1, remaining))
    print(f"{args.job_id}: watch deadline reached; resume by job ID", file=sys.stderr)
    return 1


async def execute(args: argparse.Namespace) -> int:
    async with UpgradeClient(args.api_url, SecretStr(args.token)) as client:
        verb = args.upgrade_command
        if verb == "releases":
            value: BaseModel = await client.releases(
                cursor=str(args.cursor) if args.cursor else None,
                kind=UpgradeKind(args.kind) if args.kind else None,
            )
        elif verb == "expired-recovery":
            value = await client.create_recovery(input_model(UpgradeExpiredRecoveryRequest))
        elif verb == "recovery-status":
            value = await client.recovery_status(args.recovery_id)
        elif verb == "recovery-cancel":
            value = await client.cancel_recovery(
                args.recovery_id, UpgradeExpiredRecoveryCancelRequest(intent_sha256=args.intent_sha)
            )
        elif verb == "recovery-close-unstarted":
            value = await client.close_unstarted_recovery(
                args.recovery_id, input_model(UpgradeExpiredRecoveryUnstartedCloseRequest)
            )
        elif verb == "unclaimed-cancel":
            value = await client.request_unclaimed_cancellation(
                args.job_id, input_model(UpgradeUnclaimedCancellationRequest)
            )
        elif verb == "recovery-native-terminal":
            value = await client.prepare_native_terminal(
                args.recovery_id, input_model(UpgradeNativeTerminalRequest)
            )
        elif verb == "recovery-host-hold":
            value = await client.reconcile_recovery_host_hold(
                args.recovery_id, input_model(UpgradeRecoveryHostHoldRequest)
            )
        elif verb == "recovery-schema":
            value = await client.recovery_schema(args.job_id)
        elif verb == "recovery-schema-prepare":
            value = await client.prepare_recovery_schema(input_model(UpgradeRecoverySchemaRequest))
        elif verb == "recovery-continue-cold":
            value = await client.continue_cold_recovery(input_model(UpgradeColdContinuationRequest))
        elif verb == "recovery-cold-schema":
            value = await client.cold_continuation_schema(
                args.recovery_id, input_model(UpgradeColdContinuationRead)
            )
        elif verb == "recovery-cold-schema-prepare":
            value = await client.prepare_cold_continuation_schema(
                input_model(UpgradeColdContinuationSchemaRequest)
            )
        elif verb == "versions":
            value = await client.targets()
        elif verb == "executor-handover":
            handover = CoreHandoverStartRequest(
                release_id=args.release,
                expected_incumbent_seed_sha256=args.expected_incumbent_seed,
                idempotency_key=args.idempotency_key,
            )
            print(f"Request key: {handover.idempotency_key}; reuse on retry", file=sys.stderr)
            value = await client.start_handover(handover)
        elif verb == "executor-status":
            value = await client.handover_status(args.handover_id)
        elif verb == "status":
            if args.watch:
                return await watch(client, args)
            value = await client.status(args.job_id)
        elif verb == "cancel":
            value = await client.cancel(args.job_id, UpgradeCancelRequest(reason=args.reason))
        elif verb == "rollback":
            request = UpgradeRollbackRequest(
                idempotency_key=args.idempotency_key,
                expected_source_sha256=args.expected_source,
                acknowledge_maintenance=args.acknowledge_maintenance,
            )
            print(f"Request key: {request.idempotency_key}; reuse on retry", file=sys.stderr)
            value = await client.rollback(args.job_id, request)
        elif verb in {"node", "control-plane"}:
            start = UpgradeStartRequest(
                release_id=args.release,
                expected_source_sha256=args.expected_source,
                idempotency_key=args.idempotency_key,
                acknowledge_maintenance=args.acknowledge_maintenance,
            )
            print(f"Request key: {start.idempotency_key}; reuse on retry", file=sys.stderr)
            value = (
                await client.start_studio(args.node, start)
                if verb == "node"
                else await client.start_core(start)
            )
        else:
            raise ValueError("Unsupported upgrade command")
        emit(value, json_output=args.json_output)
        return 0


def command(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(execute(args))
    except CoireError as error:
        print(f"{error.code}: {error.detail or error.title}", file=sys.stderr)
        return 1
