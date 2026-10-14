"""New recovery authority cannot rewrite the expired operation's budget."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError


def payload():
    return {
        "original_job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "original_generation": 57,
        "original_plan_sha256": "a" * 64,
        "original_scope_sha256s": ["b" * 64],
        "expected_source_sha256": "c" * 64,
        "support_release_id": str(uuid4()),
        "idempotency_key": "separate-recovery-intent-1",
        "recovery_s": 300,
    }


def intent_payload():
    from datetime import UTC, datetime, timedelta

    from upgrade_fixtures import plan

    original = plan()
    request = payload() | {
        "original_generation": original.operation.generation,
        "original_plan_sha256": original.authority_sha256(),
        "expected_source_sha256": original.source.manifest_sha256,
        "support_release_id": str(original.target.release_id),
    }
    now = datetime.now(UTC)
    return {
        "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "request": request,
        "original_plan": original.model_dump(mode="json"),
        "support_release": original.target.model_dump(mode="json"),
        "accepted_by": str(uuid4()),
        "accepted_at": now.isoformat(),
        "original_recovery_deadline_at": (now - timedelta(hours=1)).isoformat(),
        "deadline_at": (now + timedelta(seconds=300)).isoformat(),
    }


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "same_job",
        "unexpired",
        "renewed",
        "source",
        "generation",
        "support",
        "naive",
        "incompatible_support",
    ],
)
def test_server_captured_recovery_intent_binds_original_and_new_epochs(fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent

    body = intent_payload()
    if fault == "same_job":
        body["recovery_id"] = body["request"]["original_job_id"]
    elif fault == "unexpired":
        body["original_recovery_deadline_at"] = body["deadline_at"]
    elif fault == "renewed":
        from datetime import datetime, timedelta

        body["deadline_at"] = (
            datetime.fromisoformat(body["deadline_at"]) + timedelta(microseconds=1)
        ).isoformat()
    elif fault in {"source", "generation", "support"}:
        body["request"].update(
            {
                "source": {"expected_source_sha256": "f" * 64},
                "generation": {"original_generation": 999},
                "support": {"support_release_id": str(uuid4())},
            }[fault]
        )
    elif fault == "incompatible_support":
        from coire_core.models.upgrades import UpgradeReleaseManifest

        manifest = body["support_release"]["manifest"]
        manifest["compatibility"]["source_sha256s"] = ["f" * 64]
        body["support_release"]["manifest_sha256"] = UpgradeReleaseManifest.model_validate_json(
            json.dumps(manifest)
        ).sha256()
    elif fault == "naive":
        body["accepted_at"] = body["accepted_at"].split("+")[0]
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoveryIntent.model_validate_json(json.dumps(body))
    else:
        value = UpgradeExpiredRecoveryIntent.model_validate_json(json.dumps(body))
        assert value.recovery_id != value.request.original_job_id
        assert value.original_recovery_deadline_at < value.accepted_at < value.deadline_at
        assert UpgradeExpiredRecoveryIntent.model_validate_json(value.model_dump_json()) == value


def test_expired_recovery_request_is_scoped_and_roundtrips():
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryRequest

    value = UpgradeExpiredRecoveryRequest.model_validate_json(json.dumps(payload()))
    assert value.original_generation == 57
    assert UpgradeExpiredRecoveryRequest.model_validate_json(value.model_dump_json()) == value
    assert (
        value.sha256()
        == UpgradeExpiredRecoveryRequest.model_validate_json(value.model_dump_json()).sha256()
    )


@pytest.mark.parametrize(
    "fault",
    ["deadline", "command", "generation", "budget", "duplicate_scope", "url"],
)
def test_expired_recovery_request_cannot_supply_effect_or_renew_old_authority(fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryRequest

    body = payload()
    body.update(
        {
            "deadline": {"original_recovery_deadline_at": "2099-01-01T00:00:00Z"},
            "command": {"command": "launch"},
            "generation": {"original_generation": 0},
            "budget": {"recovery_s": 301},
            "duplicate_scope": {"original_scope_sha256s": ["b" * 64, "b" * 64]},
            "url": {"support_url": "http://foreign"},
        }[fault]
    )
    with pytest.raises(ValidationError):
        UpgradeExpiredRecoveryRequest.model_validate_json(json.dumps(body))
