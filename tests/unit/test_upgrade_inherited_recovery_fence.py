"""Released original claim, held recovery fence and untransferred archive custody."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_retained_source import continuation_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationContext,
    UpgradeColdContinuationObserverInput,
    UpgradeColdContinuationRead,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


def inherited_observer_fixture(tmp_path, monkeypatch):
    from coire_node import expired_recovery_continuation_observer as worker

    selector, _, launcher, settings, owner, bootstrap, run, _, _, control = continuation_fixture(
        tmp_path, monkeypatch, post_handback=True
    )
    with (
        bootstrap.ownership("lifetime.lock") as lifetime,
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, owner.sha256(), descriptor, lifetime_descriptor=lifetime),
    ):
        selected = selector.project_cold_source(settings, owner, control)
        selector.publish_cold_source_readiness(settings, selected, control)
        source = launcher.launch_cold_source(settings, selected, control)
    previous = control._read("fixture", UpgradeColdContinuationContext)
    now = owner.intent.deadline_at + timedelta(seconds=1)
    before = previous.data_baseline.model_copy(
        update={"recovery_id": owner.intent.recovery_id, "observed_at": owner.intent.accepted_at}
    )
    context = UpgradeColdContinuationContext(
        intent=owner.intent,
        owner=owner,
        data_baseline=before,
        native_baseline=previous.native_baseline,
        current_data=before.model_copy(update={"observed_at": now}),
        observed_at=now,
    )
    control._read = Mock(
        side_effect=lambda _p, model, *_a, **kw: (
            context if model is UpgradeColdContinuationContext else kw["payload"]
        )
    )
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    for name in ("local_inventory", "peer_inventory"):
        inventory = getattr(control, name)()
        setattr(control, name, Mock(return_value=inventory.model_copy(update={"observed_at": now})))
    monkeypatch.setattr(
        worker,
        "capture_native_data",
        lambda *_a, **_k: previous.native_baseline.model_copy(update={"observed_at": now}),
    )
    body = UpgradeColdContinuationObserverInput(
        previous_recovery_id=owner.intent.recovery_id,
        read=UpgradeColdContinuationRead(
            previous_intent_sha256=owner.intent.sha256(), previous_owner_sha256=owner.sha256()
        ),
        observer_release=owner.intent.support_release,
        observer_runtime=owner.runtime,
    )
    return worker, settings, body, owner, source, bootstrap, run, control


def inherited_continuation_fixture(tmp_path, monkeypatch):
    from coire_core.models.upgrade_recovery import (
        UpgradeExpiredRecoveryColdClaim,
        UpgradeExpiredRecoveryIntent,
    )
    from coire_core.models.upgrade_recovery_continuation import UpgradeColdContinuationGrant
    from coire_core.models.upgrades import NodeAgentProcessIdentity
    from coire_node import expired_recovery_cold_launch as launcher
    from coire_node import expired_recovery_cold_owner as physical
    from coire_node import expired_recovery_cold_source as selector

    worker, settings, body, previous, _, bootstrap, old_run, control = inherited_observer_fixture(
        tmp_path, monkeypatch
    )
    proof = worker.observe_continuation(settings, body, control)
    now = proof.observed_at + timedelta(seconds=1)
    payload = previous.intent.model_dump(mode="json")
    payload.pop("local_recovery_maintenance", None)
    intent = UpgradeExpiredRecoveryIntent.model_validate(
        payload
        | {
            "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FB0",
            "accepted_at": now,
            "deadline_at": now + timedelta(seconds=300),
            "inherited_local_maintenance": proof.binding.previous_local_recovery_maintenance.model_dump(
                mode="json"
            ),
        }
    )
    grant = UpgradeColdContinuationGrant(
        grant_id="01ARZ3NDEKTSV4RRFFQ69G5FB1",
        request_sha256="c" * 64,
        intent=intent,
        binding=proof.binding,
        issued_by=intent.accepted_by,
        issued_at=now,
    )
    actor = NodeAgentProcessIdentity(
        pid=555007,
        create_time_s=now.timestamp(),
        environment_sha256=previous.runtime.environment_sha256,
    )
    owner = UpgradeExpiredRecoveryColdClaim(
        intent=intent,
        process=actor,
        runtime=previous.runtime,
        kernel_boot_identity=previous.kernel_boot_identity,
        original_claim=previous.original_claim,
        original_native=previous.original_native,
        previous_guard=proof.binding.previous_guard,
        last_node=proof.binding.retained_node,
        supervision="fixture",
        retired_supervisor=previous.retired_supervisor,
        claimed_at=now,
        continuation=proof.binding,
    )
    run = PrivateUpgradeStore(old_run.root.parent / intent.recovery_id)
    run.write("cold.claim.json", owner)
    run.write("cold.activation.json", owner)
    for module in (selector, physical, launcher):
        monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(physical, "verify_passive_support_identity", lambda *_: actor)
    control.verify_support_context = Mock(return_value=intent)
    control.read_cold_recovery_continuation = Mock(return_value=grant)
    control.verify_cold_recovery_owner = Mock(side_effect=lambda value: value)
    return (
        selector,
        physical,
        launcher,
        settings,
        owner,
        bootstrap,
        run,
        tmp_path / "graphs/envs/current",
        proof.previous_source,
        control,
    )


@pytest.mark.parametrize(
    "interruption",
    [None, "intent", "selection", "accepted", "claim", "runtime", "helper", "receipt"],
)
def test_inherited_fence_archive_transfer_and_handback_preserve_released_claim(
    tmp_path, monkeypatch, interruption
):
    from test_upgrade_recovery_cold_handback import perform_source_handback_fixture

    perform_source_handback_fixture(
        tmp_path, monkeypatch, continuation=True, inherited=True, interruption=interruption
    )


@pytest.mark.parametrize("lost_ack", [False, True])
def test_inherited_owner_obtains_actual_exclusion_without_rewriting_fence(
    tmp_path, monkeypatch, lost_ack
):
    from coire_core.models.upgrades import UpgradeMaintenance

    _, physical, _, settings, expected, bootstrap, run, _, source, control = (
        inherited_continuation_fixture(tmp_path, monkeypatch)
    )
    run.path("cold.claim.json").unlink()
    run.path("cold.activation.json").unlink()
    node = PrivateUpgradeStore(tmp_path / "state/upgrades")
    before = node.path("maintenance.json").read_bytes()
    claim_before = bootstrap.path("claim.json").read_bytes()
    original = PrivateUpgradeStore(
        run.root.parent / expected.continuation.untransferred_archive_custodian
    )
    assert (original.root / "collective-history").is_dir()

    def accept(owner):
        assert owner.intent.inherited_local_maintenance == node.read(
            "maintenance.json", UpgradeMaintenance
        )
        assert owner.last_node.process == source.process
        with pytest.raises(UpgradeConflict), bootstrap.ownership("lifetime.lock"):
            pass
        with pytest.raises(UpgradeConflict), bootstrap.ownership("recovery-transfer.lock"):
            pass
        if lost_ack:
            raise UpgradeUnavailable("lost inherited acceptance")
        return owner

    control.accept_cold_recovery_owner = Mock(side_effect=accept)
    if lost_ack:
        with (
            pytest.raises(UpgradeUnavailable, match="lost inherited"),
            physical.initial_cold_owner(settings, expected.intent, expected.runtime, control),
        ):
            pytest.fail("lost acceptance yielded ownership")
        assert (
            run.path("cold.claim.json").exists() and not run.path("cold.activation.json").exists()
        )
    else:
        with physical.initial_cold_owner(
            settings, expected.intent, expected.runtime, control
        ) as owner:
            assert owner.intent == expected.intent
    assert node.path("maintenance.json").read_bytes() == before
    assert bootstrap.path("claim.json").read_bytes() == claim_before
    assert (original.root / "collective-history").is_dir()


@pytest.mark.parametrize("fault", [None, "foreign_fence", "archive_bytes"])
def test_inherited_fence_observation_requires_actual_prior_archive_custodian(
    tmp_path, monkeypatch, fault
):
    from coire_core.models.upgrades import UpgradeMaintenance

    worker, settings, body, owner, source, _bootstrap, run, control = inherited_observer_fixture(
        tmp_path, monkeypatch
    )
    node = PrivateUpgradeStore(tmp_path / "state/upgrades")
    fence = node.read("maintenance.json", UpgradeMaintenance)
    old = PrivateUpgradeStore(run.root.parent / owner.continuation.previous_recovery_id)
    if fault == "foreign_fence":
        node.write(
            "maintenance.json", fence.model_copy(update={"generation": fence.generation + 1})
        )
    elif fault == "archive_bytes":
        path = next((old.root / "collective-history").rglob("scope.json"))
        path.write_bytes(path.read_bytes() + b" ")
    if fault is not None:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            worker.observe_continuation(settings, body, control)
    else:
        proof = worker.observe_continuation(settings, body, control)
        assert proof.binding.previous_local_recovery_maintenance == fence
        assert proof.binding.original_claim == owner.original_claim
        assert proof.binding.original_claim.state == "released"
        assert proof.binding.untransferred_archive_custodian == old.root.name
        assert (
            proof.binding.untransferred_archive_sha256 == owner.continuation.previous_archive_sha256
        )
        assert proof.previous_archive is None
        assert proof.previous_source == source
        assert proof.local.job_id == fence.job_id and proof.local.generation == fence.generation
