"""Five-second authenticated cold-continuation metadata, never effect ownership."""

import threading
import time
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.upgrades.cold_continuation_context import read_cold_predecessor_context
from coire_api.upgrades.persistence import get_release
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationObservation,
    UpgradeColdContinuationRead,
)
from coire_core.upgrade_recovery_inventory import verify_retained_collective_inventory


async def verify_observation_context(
    session: AsyncSession, proof: UpgradeColdContinuationObservation, node: str
) -> None:
    if proof.previous_owner.original_claim.node != node:
        raise UpgradeForbidden()
    read = UpgradeColdContinuationRead(
        previous_intent_sha256=proof.binding.previous_intent_sha256,
        previous_owner_sha256=proof.binding.previous_owner_sha256,
    )
    context = await read_cold_predecessor_context(
        session, proof.binding.previous_recovery_id, read, node
    )
    post = proof.binding.post_handback
    handback = context.previous_handback
    partial = context.previous_handback_intent
    helper = proof.previous_unreceipted_helper
    has_writer = handback is not None or partial is not None
    held = proof.binding.previous_local_recovery_maintenance
    pending = (
        proof.binding.untransferred_archive_custodian,
        proof.binding.untransferred_archive_sha256,
        proof.binding.untransferred_history_sha256,
    )
    previous_binding = context.owner.continuation
    if pending[0] is not None:
        if previous_binding is None:
            raise UpgradeConflict("Cold continuation invents pending archive custody")
        expected_pending = (
            (
                previous_binding.previous_recovery_id,
                previous_binding.previous_archive_sha256,
                previous_binding.previous_history_sha256,
            )
            if previous_binding.previous_archive_sha256 is not None
            else (
                previous_binding.untransferred_archive_custodian,
                previous_binding.untransferred_archive_sha256,
                previous_binding.untransferred_history_sha256,
            )
        )
        if pending != expected_pending:
            raise UpgradeConflict("Cold continuation changes recorded pending archive custody")
    if held is not None and held != context.intent.local_maintenance():
        raise UpgradeConflict("Cold continuation changes held local recovery scope")
    if (
        context.intent.local_maintenance() != context.intent.original_plan.maintenance
        and not has_writer
        and held is None
    ):
        raise UpgradeConflict("Cold continuation omitted held recovery fence")
    if (proof.unstarted_startup is None) != (context.unstarted_intent is None) or (
        proof.unstarted_startup is not None
        and proof.unstarted_startup.intent != context.unstarted_intent
    ):
        raise UpgradeConflict("Cold unstarted custody evidence differs from Core")
    if (post is None) != (not has_writer):
        raise UpgradeConflict("Cold continuation handback boundary differs from Core")
    if partial is not None and (
        post is None
        or post.stage not in {"ready_unreceipted", "keeper_retired"}
        or helper is None
        or helper.handback != partial
        or helper.sha256() != post.helper_sha256
        or partial.sha256() != post.handback_sha256
        or post.released_claim != partial.released_claim
        or post.source_helper.process != helper.process
        or not post.released_maintenance.matches_plan(context.intent.local_inventory_plan())
    ):
        raise UpgradeConflict("Cold continuation partial ready Source differs from Core")
    if (
        proof.unstarted_custody_grant is not None
        and proof.unstarted_custody_grant != context.unstarted_grant
    ):
        raise UpgradeConflict("Keeper continuation lineage differs from Core")
    keeper = proof.unstarted_keeper_retirement
    if keeper is not None:
        from sqlalchemy import select

        from coire_api.db import UpgradeRecoverySuccessionRow

        stored = await session.scalar(
            select(UpgradeRecoverySuccessionRow).where(
                UpgradeRecoverySuccessionRow.recovery_id == keeper.grant.intent.recovery_id
            )
        )
        if (
            (
                context.unstarted_intent != keeper.grant.intent
                and (
                    proof.unstarted_custody_grant is None
                    or proof.unstarted_custody_grant != context.unstarted_grant
                )
            )
            or stored is None
            or stored.grant_sha256 != keeper.grant.sha256()
            or stored.grant_payload != keeper.grant.model_dump(mode="json")
        ):
            raise UpgradeConflict("Keeper preclaim custody differs from audited Core grant")
    if partial is None and helper is not None:
        raise UpgradeConflict("Cold continuation invents partial handback")
    if (
        post is not None
        and handback is not None
        and (
            post.handback_sha256 != handback.sha256()
            or post.released_claim != handback.helper.handback.released_claim
            or post.source_helper.process != handback.helper.process
            or post.source_helper.selected != handback.status.selected
            or post.released_maintenance.model_dump(exclude={"state", "updated_at"})
            != context.intent.local_maintenance().model_dump(exclude={"state", "updated_at"})
        )
    ):
        raise UpgradeConflict("Cold continuation released Source preimages differ")
    if (
        proof.previous_owner != context.owner
        or proof.binding.core_data_baseline_sha256 != context.data_baseline.sha256()
        or proof.native_baseline != context.native_baseline
        or proof.binding.previous_guard.process
        != (
            handback.helper.process
            if handback is not None
            else helper.process
            if helper is not None
            else context.owner.process
        )
        or proof.binding.previous_guard.bootstrap_runtime
        != (context.owner.original_native.source.runtime if has_writer else context.owner.runtime)
        or not 0 <= (datetime.now(UTC) - proof.observed_at).total_seconds() <= 5
        or (proof.previous_smoke.smoke if proof.previous_smoke is not None else None)
        != context.previous_smoke_command
    ):
        raise UpgradeConflict("Cold continuation observation changes retained predecessor")
    verify_retained_collective_inventory(
        context.intent.original_plan,
        context.intent.request.original_scope_sha256s,
        proof.scopes,
        ()
        if proof.previous_archive is not None
        or proof.binding.untransferred_archive_custodian is not None
        else proof.interrupted_scope_sha256s,
        proof.local,
        proof.peer,
        defer_local=post is not None,
    )
    support = await get_release(session, proof.observer_release.release_id)
    if support != proof.observer_release:
        raise UpgradeConflict("Cold continuation observer differs from immutable catalog")


class ColdContinuationObservationRelay:
    """Bound cache only. Final acceptance must recheck all durable context."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, tuple[UpgradeColdContinuationObservation, float, datetime]] = {}

    def publish(self, proof: UpgradeColdContinuationObservation) -> None:
        proof = UpgradeColdContinuationObservation.model_validate_json(proof.model_dump_json())
        now, tick = datetime.now(UTC), time.monotonic()
        if not 0 <= (now - proof.observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Cold continuation observation is stale or future")
        with self._lock:
            self._values = {
                key: value
                for key, value in self._values.items()
                if value[1] <= tick < value[1] + 5 and 0 <= (now - value[2]).total_seconds() <= 5
            }
            digest = proof.sha256()
            if digest not in self._values and len(self._values) >= 16:
                raise UpgradeUnavailable("Cold continuation metadata cache exceeds bound")
            self._values[digest] = proof, tick, now

    def read(self, digest: str) -> UpgradeColdContinuationObservation:
        now, tick = datetime.now(UTC), time.monotonic()
        with self._lock:
            value = self._values.get(digest)
            if (
                value is None
                or not value[1] <= tick < value[1] + 5
                or not 0 <= (now - value[2]).total_seconds() <= 5
            ):
                raise UpgradeUnavailable("Cold continuation observation is unavailable or expired")
            proof = value[0]
            if not 0 <= (now - proof.observed_at).total_seconds() <= 5:
                raise UpgradeUnavailable("Cold continuation observation epoch differs")
            return proof
