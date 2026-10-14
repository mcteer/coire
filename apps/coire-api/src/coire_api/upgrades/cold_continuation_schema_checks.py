"""Fixed known preimages and additive separate-recovery supersession state."""

RECOVERY_PHASES = (
    "accepted",
    "transferring",
    "transfer_consumed",
    "recovering",
    "verifying",
    "cold_recovering",
    "handback_prepared",
    "handing_back",
    "resolving",
    "recovery_required",
    "recovered",
    "cancelled_unchanged",
)
LEGACY_SUPERSESSION_PAYLOAD_CHECK = "phase <> 'superseded' OR (cold_claim_payload IS NOT NULL AND cancellation_payload IS NOT NULL AND resolution_payload IS NOT NULL)"
SUPERSESSION_PAYLOAD_CHECK = "phase <> 'superseded' OR (cancellation_payload IS NOT NULL AND resolution_payload IS NOT NULL AND (cold_claim_payload IS NOT NULL OR coalesce((resolution_payload->>'protocol' = 'cold-unstarted-supersession-v1' AND resolution_payload->'unstarted_startup'->'intent' = intent_payload AND transfer_payload IS NULL AND consumption_payload IS NULL AND owner_claim_payload IS NULL AND cold_claim_payload IS NULL AND cold_smoke_payload IS NULL AND cold_verification_payload IS NULL AND cold_handback_intent_payload IS NULL AND cold_handback_receipt_payload IS NULL),false)))"


def known_checks(*, continuation: bool, unstarted: bool = True) -> dict[str, str]:
    phases = (*RECOVERY_PHASES, "superseded") if continuation else RECOVERY_PHASES
    terminals = (
        ("recovered", "cancelled_unchanged", "superseded")
        if continuation
        else ("recovered", "cancelled_unchanged")
    )
    phase_array = ", ".join(f"'{phase}'::character varying" for phase in phases)
    terminal_array = ", ".join(f"'{phase}'::character varying" for phase in terminals)
    result = {
        "ck_expired_recovery_phase": f"CHECK (((phase)::text = ANY ((ARRAY[{phase_array}])::text[])))",
        "ck_expired_recovery_terminal_owner": f"CHECK (((active_slot IS NULL) = ((phase)::text = ANY ((ARRAY[{terminal_array}])::text[]))))",
    }
    cancellation = (
        ("recovery_required", "recovered", "superseded")
        if continuation
        else ("recovery_required", "recovered")
    )
    cancel_array = ", ".join(f"'{phase}'::character varying" for phase in cancellation)
    result["ck_expired_recovery_cancel_retained"] = (
        f"CHECK (((cancellation_payload IS NULL) OR ((phase)::text = ANY ((ARRAY[{cancel_array}])::text[]))))"
    )
    if continuation:
        result["ck_cold_recovery_supersession_payload"] = (
            "CHECK ((((phase)::text <> 'superseded'::text) OR "
            "((cold_claim_payload IS NOT NULL) AND (cancellation_payload IS NOT NULL) "
            "AND (resolution_payload IS NOT NULL))))"
        )
    if continuation and unstarted:
        result["ck_cold_recovery_supersession_payload"] = (
            "CHECK ((((phase)::text <> 'superseded'::text) OR ((cancellation_payload IS NOT NULL) AND (resolution_payload IS NOT NULL) AND ((cold_claim_payload IS NOT NULL) OR COALESCE((((resolution_payload ->> 'protocol'::text) = 'cold-unstarted-supersession-v1'::text) AND (((resolution_payload -> 'unstarted_startup'::text) -> 'intent'::text) = intent_payload) AND (transfer_payload IS NULL) AND (consumption_payload IS NULL) AND (owner_claim_payload IS NULL) AND (cold_claim_payload IS NULL) AND (cold_smoke_payload IS NULL) AND (cold_verification_payload IS NULL) AND (cold_handback_intent_payload IS NULL) AND (cold_handback_receipt_payload IS NULL)), false)))))"
        )
    return result


def continuation_ddl() -> tuple[str, ...]:
    phases = ",".join(f"'{phase}'" for phase in (*RECOVERY_PHASES, "superseded"))
    return (
        "ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT IF EXISTS ck_cold_recovery_supersession_payload",
        "ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT ck_expired_recovery_phase",
        "ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT ck_expired_recovery_terminal_owner",
        "ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT ck_expired_recovery_cancel_retained",
        "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_phase "
        f"CHECK (phase IN ({phases}))",
        "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_terminal_owner "
        "CHECK ((active_slot IS NULL) = (phase IN ('recovered','cancelled_unchanged','superseded')))",
        "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_cancel_retained "
        "CHECK (cancellation_payload IS NULL OR phase IN ('recovery_required','recovered','superseded'))",
        "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_cold_recovery_supersession_payload "
        f"CHECK ({SUPERSESSION_PAYLOAD_CHECK})",
    )
