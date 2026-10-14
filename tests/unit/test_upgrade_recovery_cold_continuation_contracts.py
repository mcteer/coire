"""Separate captured budgets never rewrite predecessor or original authority."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_cold_continuation_binding import binding_fixture

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationGrant,
    UpgradeColdContinuationRequest,
    UpgradeColdRecoverySupersession,
)


def grant_fixture(tmp_path, monkeypatch, *, with_owner=False):
    binding, owner, _source = binding_fixture(tmp_path, monkeypatch, with_context=True)
    accepted = binding.observed_at + timedelta(seconds=1)
    intent = UpgradeExpiredRecoveryIntent.model_validate(
        owner.intent.model_copy(
            update={
                "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
                "accepted_at": accepted,
                "deadline_at": accepted + timedelta(seconds=owner.intent.request.recovery_s),
            }
        ).model_dump(mode="json")
    )
    request = UpgradeColdContinuationRequest(
        previous_recovery_id=binding.previous_recovery_id,
        previous_intent_sha256=binding.previous_intent_sha256,
        previous_owner_sha256=binding.previous_owner_sha256,
        observation_sha256="c" * 64,
        request=intent.request,
    )
    grant = UpgradeColdContinuationGrant(
        grant_id="01ARZ3NDEKTSV4RRFFQ69G5FAY",
        request_sha256=request.sha256(),
        intent=intent,
        binding=binding,
        issued_by=intent.accepted_by,
        issued_at=accepted,
    )
    return (request, grant, owner) if with_owner else (request, grant)


def test_distinct_grant_preserves_predecessor_deadline_and_original_failure(tmp_path, monkeypatch):
    request, grant = grant_fixture(tmp_path, monkeypatch)
    assert UpgradeColdContinuationGrant.model_validate_json(grant.model_dump_json()) == grant
    receipt = UpgradeColdRecoverySupersession(
        previous_recovery_id=request.previous_recovery_id,
        previous_intent_sha256=request.previous_intent_sha256,
        previous_owner_sha256=request.previous_owner_sha256,
        previous_deadline_at=grant.binding.previous_deadline_at,
        next_recovery_id=grant.intent.recovery_id,
        next_intent_sha256=grant.intent.sha256(),
        grant_sha256=grant.sha256(),
        original_job_id=request.request.original_job_id,
        original_generation=request.request.original_generation,
        superseded_by=grant.issued_by,
        superseded_at=grant.issued_at,
    )
    assert receipt.phase == "superseded"
    assert receipt.original_upgrade_succeeded is False
    assert receipt.original_maintenance_retained is True
    assert receipt.previous_deadline_at < grant.intent.accepted_at


@pytest.mark.parametrize(
    "fault", ["same_root", "early", "overdue", "wrong_source", "wrong_actor", "extra"]
)
def test_continuation_grant_refuses_foreign_or_renewed_original_authority(
    tmp_path, monkeypatch, fault
):
    _request, grant = grant_fixture(tmp_path, monkeypatch)
    body = grant.model_dump(mode="json")
    if fault == "same_root":
        body["intent"]["recovery_id"] = grant.binding.previous_recovery_id
    elif fault == "early":
        body["issued_at"] = (grant.intent.accepted_at - timedelta(seconds=1)).isoformat()
    elif fault == "overdue":
        body["issued_at"] = grant.intent.deadline_at.isoformat()
    elif fault == "wrong_source":
        body["intent"]["request"]["expected_source_sha256"] = "e" * 64
    elif fault == "wrong_actor":
        body["issued_by"] = "00000000-0000-4000-8000-000000000001"
    else:
        body["renew_original_deadline"] = True
    with pytest.raises(ValidationError):
        UpgradeColdContinuationGrant.model_validate(body)


def test_continuation_request_cannot_borrow_preservation_mode(tmp_path, monkeypatch):
    request, _grant = grant_fixture(tmp_path, monkeypatch)
    body = request.model_dump(mode="json")
    body["request"]["recovery_mode"] = "preserve"
    with pytest.raises(ValidationError):
        UpgradeColdContinuationRequest.model_validate(body)


def test_continuation_owner_binds_live_retained_source_and_new_epoch(tmp_path, monkeypatch):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdClaim
    from coire_core.models.upgrades import NodeAgentProcessIdentity

    _request, grant, previous = grant_fixture(tmp_path, monkeypatch, with_owner=True)
    owner = UpgradeExpiredRecoveryColdClaim(
        intent=grant.intent,
        process=NodeAgentProcessIdentity(
            pid=555006,
            create_time_s=grant.intent.accepted_at.timestamp(),
            environment_sha256=previous.runtime.environment_sha256,
        ),
        runtime=previous.runtime,
        kernel_boot_identity=grant.binding.previous_kernel_boot_identity,
        original_claim=grant.binding.original_claim,
        original_native=grant.binding.original_native,
        previous_guard=grant.binding.previous_guard,
        last_node=grant.binding.retained_node,
        supervision="fixture",
        retired_supervisor=previous.retired_supervisor,
        claimed_at=grant.intent.accepted_at,
        continuation=grant.binding,
    )
    assert owner.last_node.process.create_time_s < owner.intent.accepted_at.timestamp()
    assert owner.original_native.recovery_deadline_at == grant.intent.original_recovery_deadline_at
    assert owner.continuation.previous_recovery_id != owner.intent.recovery_id
    assert UpgradeExpiredRecoveryColdClaim.model_validate_json(owner.model_dump_json()) == owner


def test_absent_continuation_preserves_old_canonical_owner_digest(tmp_path, monkeypatch):
    import hashlib
    import json

    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdClaim

    _binding, owner, _source = binding_fixture(tmp_path, monkeypatch, with_context=True)
    legacy_body = owner.model_dump(mode="json")
    assert "continuation" not in legacy_body
    legacy_digest = hashlib.sha256(
        json.dumps(legacy_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert owner.sha256() == legacy_digest
    parsed = UpgradeExpiredRecoveryColdClaim.model_validate({**legacy_body, "continuation": None})
    assert parsed.sha256() == legacy_digest


@pytest.mark.parametrize("fault", [None, "missing_dispatch", "unknown_local", "busy_peer"])
def test_zero_dispatch_continuation_still_requires_exact_plan_and_complete_inventory(
    tmp_path, monkeypatch, fault
):
    from test_upgrade_recovery_cold_archive import archive_fixture

    from coire_core.errors import UpgradeUnavailable
    from coire_core.upgrade_recovery_inventory import verify_retained_collective_inventory

    _, _, _, owner, _, _, _, _, _, control = archive_fixture(tmp_path, monkeypatch)
    local = control.local_inventory()
    local = local.model_copy(
        update={"owners": tuple(o.model_copy(update={"entries": ()}) for o in local.owners)}
    )
    peer = control.peer_inventory()
    if fault == "unknown_local":
        local = local.model_copy(
            update={
                "owners": tuple(o.model_copy(update={"status": "unknown"}) for o in local.owners)
            }
        )
    if fault == "busy_peer":
        peer = peer.model_copy(
            update={
                "owners": tuple(o.model_copy(update={"status": "blocked"}) for o in peer.owners)
            }
        )
    expected = owner.intent.request.original_scope_sha256s if fault == "missing_dispatch" else ()
    if fault:
        with pytest.raises(UpgradeUnavailable):
            verify_retained_collective_inventory(
                owner.intent.original_plan, expected, (), (), local, peer
            )
    else:
        verify_retained_collective_inventory(
            owner.intent.original_plan, expected, (), (), local, peer
        )
