"""Continuation metadata binds an exact prior Source without embedding a chain."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_cold_source import source_fixture

from coire_core.models.upgrade_recovery_cold_binding import UpgradeColdContinuationBinding
from coire_core.models.upgrades import NativeNodeProcess, NodeAgentProcessIdentity
from coire_node.recovery_lease import bind_recovery_lease


def binding_fixture(tmp_path, monkeypatch, *, with_context=False):
    worker, settings, claim, bootstrap, _run, _guard, _current, control = source_fixture(
        tmp_path, monkeypatch, supervision="fixture"
    )
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        source = worker.project_cold_source(settings, claim, control)
    projection = source.projection
    recorded = projection.prepared_at + timedelta(milliseconds=10)
    node = NativeNodeProcess(
        selected=projection.source_selection,
        process=NodeAgentProcessIdentity(
            pid=234567,
            create_time_s=recorded.timestamp(),
            environment_sha256=claim.original_native.source.runtime.environment_sha256,
        ),
        recorded_at=recorded,
    )
    guard = claim.previous_guard.model_copy(
        update={
            "selected": node.selected,
            "process": claim.process,
            "bootstrap_runtime": claim.runtime,
        }
    )
    deadline = claim.intent.deadline_at
    binding = UpgradeColdContinuationBinding(
        previous_recovery_id=claim.intent.recovery_id,
        previous_intent_sha256=claim.intent.sha256(),
        previous_owner_sha256=claim.sha256(),
        previous_source_sha256=source.sha256(),
        previous_deadline_at=deadline,
        previous_kernel_boot_identity=claim.kernel_boot_identity,
        retired_supervisor_sha256=claim.retired_supervisor.sha256(),
        original_claim=claim.original_claim,
        original_native=claim.original_native,
        projected_native=projection.projected_native,
        previous_guard=guard,
        retained_node=node,
        core_data_baseline_sha256="a" * 64,
        native_data_baseline_sha256="b" * 64,
        observed_at=deadline + timedelta(seconds=1),
    )
    return (binding, claim, source) if with_context else binding


def test_flat_continuation_roundtrip_retains_source_birth_and_original_budget(
    tmp_path, monkeypatch
):
    binding = binding_fixture(tmp_path, monkeypatch)
    parsed = UpgradeColdContinuationBinding.model_validate_json(binding.model_dump_json())
    assert parsed == binding
    assert parsed.retained_node.process.create_time_s == binding.retained_node.process.create_time_s
    assert parsed.original_native.recovery_deadline_at < parsed.previous_deadline_at
    assert "previous_owner" not in parsed.model_dump()
    assert "previous_source" not in parsed.model_dump()


@pytest.mark.parametrize(
    "fault", ["live_epoch", "preactivation", "projection", "node", "owner", "maintenance", "extra"]
)
def test_continuation_metadata_refuses_wrong_boundary(tmp_path, monkeypatch, fault):
    binding = binding_fixture(tmp_path, monkeypatch)
    body = binding.model_dump(mode="json")
    if fault == "live_epoch":
        body["observed_at"] = (binding.previous_deadline_at - timedelta(seconds=1)).isoformat()
    elif fault == "preactivation":
        body["original_native"]["activation_started_at"] = None
        body["original_native"]["activation_grant_id"] = None
        body["original_native"]["readiness_deadline_at"] = None
    elif fault == "projection":
        body["projected_native"]["recovery_deadline_at"] = binding.previous_deadline_at.isoformat()
    elif fault == "node":
        body["retained_node"]["selected"]["generation"] += 1
    elif fault == "owner":
        body["previous_owner_retired"] = False
    elif fault == "maintenance":
        body["original_maintenance_retained"] = False
    else:
        body["portable_lease"] = "not-kernel-ownership"
    with pytest.raises(ValidationError):
        UpgradeColdContinuationBinding.model_validate(body)
