"""One known additive constraint transition; no caller SQL or row mutation."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.upgrades.cold_continuation_schema_checks import known_checks
from coire_core.errors import UpgradeConflict

NO_EFFECTS = (
    "transfer_payload IS NULL AND consumption_payload IS NULL AND owner_claim_payload IS NULL "
    "AND cold_claim_payload IS NULL AND cold_smoke_payload IS NULL "
    "AND cold_verification_payload IS NULL AND cold_handback_intent_payload IS NULL "
    "AND cold_handback_receipt_payload IS NULL AND resolution_payload IS NULL"
)


async def prepare_cancelled_unstarted_constraint(session: AsyncSession) -> None:
    rows = {
        str(name): str(definition)
        for name, definition in (
            await session.execute(
                text(
                    "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid=to_regclass('upgrade_expired_recoveries') "
                    "AND conname IN ('ck_expired_recovery_cancel_retained',"
                    "'ck_unstarted_recovery_no_effect_authority')"
                )
            )
        ).all()
    }
    expected = known_checks(continuation=True, unstarted=True)[
        "ck_expired_recovery_cancel_retained"
    ]
    if rows != {"ck_expired_recovery_cancel_retained": expected}:
        raise UpgradeConflict("Cancelled-unstarted schema is not the exact known preimage")
    before = await session.scalar(
        text(
            "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY id),'[]'::jsonb) FROM upgrade_expired_recoveries r"
        )
    )
    await session.execute(
        text(
            "ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT ck_expired_recovery_cancel_retained"
        )
    )
    await session.execute(
        text(
            "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_cancel_retained "
            "CHECK (cancellation_payload IS NULL OR phase IN "
            "('recovery_required','recovered','superseded','cancelled_unchanged'))"
        )
    )
    await session.execute(
        text(
            "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_unstarted_recovery_no_effect_authority "
            "CHECK (phase <> 'cancelled_unchanged' OR (" + NO_EFFECTS + "))"
        )
    )
    after = await session.scalar(
        text(
            "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY id),'[]'::jsonb) FROM upgrade_expired_recoveries r"
        )
    )
    if before != after:
        raise UpgradeConflict("Cancelled-unstarted preparation changed retained recovery rows")
