"""Executor restart cannot renew effects, activation authority, or restoration budgets."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreExecutorEffect,
    UpgradeActivationConsume,
    UpgradeActivationGrant,
    UpgradeDockerContainer,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeReceiptAck,
)
from coire_upgrade_executor.journal import ExecutorJournal


@pytest.mark.parametrize("fault", [None, "missing", "stale", "future", "image", "replacement"])
def test_source_snapshot_retains_exact_recovery_identity(tmp_path: Path, fault: str | None) -> None:
    plan = core_plan()
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "snapshot", plan)
    observations = tuple(
        UpgradeDockerContainer(
            container_id=f"{index + 1:064x}",
            name=f"coire-{service.service}-1",
            service=service.service,
            project="coire",
            image_id=service.image_id,
            observed_at=now,
            running=True,
            health="healthy",
            exit_code=0,
            network_ids={"coire-internal": "b" * 64},
        )
        for index, service in enumerate(plan.source.manifest.services)
    )
    if fault == "replacement":
        journal.freeze_source(observations, now=now)
    if fault == "missing":
        observations = observations[:-1]
    elif fault in {"stale", "future"}:
        observations = tuple(
            item.model_copy(
                update={"observed_at": now + timedelta(seconds=-6 if fault == "stale" else 1)}
            )
            for item in observations
        )
    elif fault in {"image", "replacement"}:
        observations = (
            observations[0].model_copy(
                update={"image_id": "sha256:" + "f" * 64}
                if fault == "image"
                else {"container_id": "e" * 64}
            ),
            *observations[1:],
        )
    if fault is not None:
        with pytest.raises((UpgradeConflict, ValueError)):
            journal.freeze_source(observations, now=now)
        assert journal.store.path("source-snapshot.json").exists() == (fault == "replacement")
        return
    captured = journal.freeze_source(observations, now=now)
    restarted = ExecutorJournal(tmp_path / "snapshot", plan)
    assert restarted.source_snapshot() == captured
    fresh = tuple(
        item.model_copy(update={"observed_at": now + timedelta(seconds=2)}) for item in observations
    )
    assert restarted.freeze_source(fresh, now=now + timedelta(seconds=2)) == captured


def activation(journal: ExecutorJournal, now: datetime) -> UpgradeActivationConsume:
    operation = journal.plan.operation
    return UpgradeActivationConsume(
        grant=UpgradeActivationGrant(
            job_id=operation.job_id,
            generation=operation.generation,
            boundary="runtime",
            grant_id=uuid4(),
            expires_at=now + timedelta(seconds=30),
        ),
        intent=UpgradeReceipt(
            job_id=operation.job_id,
            generation=operation.generation,
            sequence=1,
            effect_id="core-runtime-activation",
            phase=UpgradePhase.ACTIVATING,
            occurred_at=now,
            outcome="intent",
            observed_sha256=operation.source_sha256,
        ),
    )


def test_failure_detection_has_its_own_fixed_restoration_budget(tmp_path: Path) -> None:
    plan = core_plan()
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "failure", plan)
    journal.activation(activation(journal, now), now=now)
    journal.capture_outage(now=now)
    detected = now + timedelta(seconds=10)
    recovery = journal.capture_recovery(now=detected)
    assert recovery.recovery_captured_at == detected
    assert recovery.recovery_deadline == detected + timedelta(seconds=300)
    assert recovery.outage_deadline == now + timedelta(seconds=300)
    assert (
        ExecutorJournal(tmp_path / "failure", plan).capture_recovery(now=now + timedelta(hours=1))
        == recovery
    )


def test_interrupted_migration_cannot_renew_recovery_on_late_restart(tmp_path: Path) -> None:
    plan = core_plan(schema_changed=True)
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "migration-recovery", plan)
    value = activation(journal, now)
    consumed = UpgradeActivationConsume.model_validate(
        value.model_dump()
        | {
            "grant": value.grant.model_dump() | {"boundary": "migration"},
            "intent": value.intent.model_dump() | {"phase": UpgradePhase.MIGRATING},
        }
    )
    journal.activation(consumed, now=now)
    recovery = ExecutorJournal(tmp_path / "migration-recovery", plan).capture_recovery(
        now=now + timedelta(hours=1)
    )
    assert recovery.recovery_captured_at == now + timedelta(seconds=120)
    assert recovery.recovery_deadline == now + timedelta(seconds=420)


def test_restart_preserves_activation_outage_and_recovery_deadlines(tmp_path: Path) -> None:
    plan = core_plan()
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "core", plan)
    consumed = activation(journal, now)
    state = journal.activation(consumed, now=now)
    assert state.readiness_deadline == now + timedelta(seconds=120)
    outage = journal.capture_outage(now=now + timedelta(seconds=1))
    restarted = ExecutorJournal(tmp_path / "core", plan)
    assert restarted.activation(consumed, now=now + timedelta(hours=1)) == outage
    assert restarted.capture_outage(now=now + timedelta(hours=1)) == outage
    recovery = restarted.capture_recovery(now=now + timedelta(hours=1))
    assert recovery.recovery_captured_at == state.readiness_deadline
    assert recovery.recovery_deadline == state.readiness_deadline + timedelta(seconds=300)
    assert (
        ExecutorJournal(tmp_path / "core", plan).capture_recovery(now=now + timedelta(hours=2))
        == recovery
    )
    with pytest.raises(UpgradeConflict):
        restarted.activation(activation(restarted, now), now=now)


def test_expired_new_activation_never_gets_a_fresh_budget(tmp_path: Path) -> None:
    journal = ExecutorJournal(tmp_path / "core", core_plan())
    now = datetime.now(UTC)
    consumed = activation(journal, now)
    with pytest.raises(UpgradeForbidden):
        journal.activation(consumed, now=now + timedelta(seconds=31))
    assert journal.state().activations == ()
    assert journal.state().readiness_deadline is None


def test_lost_consumed_acknowledgment_can_only_establish_original_recovery(
    tmp_path: Path,
) -> None:
    journal = ExecutorJournal(tmp_path / "core", core_plan())
    now = datetime.now(UTC)
    consumed = activation(journal, now)
    ack = UpgradeReceiptAck(
        job_id=consumed.intent.job_id,
        generation=consumed.intent.generation,
        sequence=consumed.intent.sequence,
    )
    late = now + timedelta(seconds=31)
    with pytest.raises(UpgradeConflict, match="acknowledgment"):
        journal.activation(
            consumed, now=late, recovery_ack=ack.model_copy(update={"sequence": ack.sequence + 1})
        )
    state = journal.activation(consumed, now=late, recovery_ack=ack)
    assert state.activations == (consumed,)
    assert state.recovery_captured_at == consumed.grant.expires_at
    assert state.recovery_deadline == consumed.grant.expires_at + timedelta(seconds=300)
    assert state.service_windows
    window = state.service_windows[0]
    effect = CoreExecutorEffect(
        job_id=consumed.intent.job_id,
        generation=consumed.intent.generation,
        plan_sha256=journal.plan.authority_sha256(),
        effect_id="forbidden-late-candidate",
        action="create_candidate",
        service=window.service,
        image_id=journal.plan.target.manifest.services[0].image_id,
        captured_at=late,
        deadline=window.deadline_at,
    )
    with pytest.raises(UpgradeForbidden, match="recovery"):
        journal.intent(effect, now=late)
    restarted = ExecutorJournal(tmp_path / "core", journal.plan)
    assert restarted.activation(consumed, now=now + timedelta(hours=1), recovery_ack=ack) == state


def test_each_service_has_an_original_readiness_window_that_restart_cannot_renew(
    tmp_path: Path,
) -> None:
    from test_executor_cohort import cohort_plan

    value = cohort_plan()
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "windows", value)
    state = journal.activation(activation(journal, now), now=now)
    assert state.readiness_deadline == now + timedelta(seconds=6 * 120)
    first = journal.service_window("coire-file-worker", now=now + timedelta(seconds=90))
    assert first.deadline_at == now + timedelta(seconds=120)
    next_window = journal.service_window("coire-ops", now=now + timedelta(seconds=110))
    assert next_window.deadline_at == now + timedelta(seconds=230)
    restarted = ExecutorJournal(tmp_path / "windows", value)
    assert restarted.service_window("coire-ops", now=now + timedelta(hours=1)) == next_window
    with pytest.raises(UpgradeForbidden):
        restarted.service_window("coire-mcp", now=now + timedelta(hours=1))
    outage = restarted.capture_outage(now=now + timedelta(seconds=200))
    assert outage.outage_deadline == now + timedelta(seconds=500)
    clipped = restarted.service_window("coire-api", now=now + timedelta(seconds=450))
    assert clipped.deadline_at == outage.outage_deadline


def test_effect_is_fsynced_before_use_and_replay_requires_same_identity(tmp_path: Path) -> None:
    journal = ExecutorJournal(tmp_path / "core", core_plan())
    now = datetime.now(UTC)
    state = journal.activation(activation(journal, now), now=now)
    assert state.readiness_deadline is not None
    effect = CoreExecutorEffect(
        job_id=state.job_id,
        generation=state.generation,
        plan_sha256=state.plan_sha256,
        effect_id="stop-api-source",
        action="stop_source",
        service="coire-api",
        container_id="c" * 64,
        image_id=journal.plan.source.manifest.services[0].image_id,
        captured_at=now,
        deadline=state.readiness_deadline,
    )
    assert journal.intent(effect, now=now) == effect
    restarted = ExecutorJournal(tmp_path / "core", journal.plan)
    assert restarted.intent(effect, now=now + timedelta(hours=1)) == effect
    with pytest.raises(UpgradeConflict):
        restarted.intent(effect.model_copy(update={"container_id": "d" * 64}), now=now)
    with pytest.raises(UpgradeForbidden):
        restarted.intent(
            effect.model_copy(update={"effect_id": "another-effect"}), now=now + timedelta(hours=1)
        )
    assert not restarted.effects.path("another-effect.json").exists()


@pytest.mark.parametrize("completed", [False, True])
def test_expired_service_detection_is_read_only_and_preserves_recovery_epoch(
    tmp_path: Path, completed: bool
) -> None:
    from test_executor_cohort import cohort_plan

    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "service-deadline", cohort_plan())
    state = journal.activation(activation(journal, now), now=now)
    window = state.service_windows[0]
    if completed:
        effect = CoreExecutorEffect(
            job_id=state.job_id,
            generation=state.generation,
            plan_sha256=state.plan_sha256,
            effect_id="core-start-candidate-" + window.service,
            action="start_candidate",
            service=window.service,
            container_id="c" * 64,
            image_id=journal.plan.target.manifest.services[0].image_id,
            captured_at=now,
            deadline=window.deadline_at,
            outcome="complete",
        )
        journal.effects.write(effect.effect_id + ".json", effect)
    before = journal.store.path("epoch.json").read_bytes()
    detected = window.deadline_at + timedelta(seconds=1)
    assert state.readiness_deadline is not None and detected < state.readiness_deadline
    expired = journal.expired_forward_deadlines(now=detected)
    assert expired == (() if completed else (window.deadline_at,))
    assert journal.store.path("epoch.json").read_bytes() == before
    if not completed:
        recovered = journal.capture_recovery(now=detected)
        assert recovered.recovery_captured_at == window.deadline_at
        assert recovered.recovery_deadline == window.deadline_at + timedelta(seconds=300)
        assert journal.capture_recovery(now=detected + timedelta(hours=1)) == recovered


def test_corrupt_epoch_is_never_replaced_with_a_new_owner(tmp_path: Path) -> None:
    plan = core_plan()
    journal = ExecutorJournal(tmp_path / "core", plan)
    journal.store.path("epoch.json").write_text("corrupt")
    with pytest.raises(UpgradeUnavailable):
        ExecutorJournal(tmp_path / "core", plan)
    assert journal.store.path("epoch.json").read_text() == "corrupt"


@pytest.mark.parametrize("fault", [None, "identity", "image", "running", "scope"])
def test_lost_response_requires_exact_observed_effect_outcome(
    tmp_path: Path, fault: str | None
) -> None:
    from coire_core.models.upgrades import UpgradeDockerContainer

    journal = ExecutorJournal(tmp_path / "core", core_plan())
    now = datetime.now(UTC)
    state = journal.activation(activation(journal, now), now=now)
    assert state.readiness_deadline is not None
    effect = CoreExecutorEffect(
        job_id=state.job_id,
        generation=state.generation,
        plan_sha256=state.plan_sha256,
        effect_id="stop-api-source",
        action="stop_source",
        service="coire-api",
        container_id="c" * 64,
        image_id=journal.plan.source.manifest.services[0].image_id,
        captured_at=now,
        deadline=state.readiness_deadline,
    )
    journal.intent(effect, now=now)
    assert journal.uncertain(effect.effect_id).outcome == "unknown"
    restarted = ExecutorJournal(tmp_path / "core", journal.plan)
    observed = UpgradeDockerContainer(
        container_id="d" * 64 if fault == "identity" else "c" * 64,
        name="coire-coire-api-1",
        service="coire-api",
        project="other" if fault == "scope" else "coire",
        image_id="sha256:" + "f" * 64 if fault == "image" else effect.image_id,
        observed_at=datetime.now(UTC),
        running=fault == "running",
        health="none",
        exit_code=0,
        network_ids={"coire-internal": "b" * 64},
    )
    if fault is None:
        completed = restarted.observe(effect.effect_id, observed)
        assert completed.outcome == "complete" and completed.observed_container_id == "c" * 64
        assert restarted.intent(effect, now=now + timedelta(hours=1)) == completed
        with pytest.raises(UpgradeConflict):
            restarted.observe(effect.effect_id, observed.model_copy(update={"running": True}))
    else:
        with pytest.raises(UpgradeConflict):
            restarted.observe(effect.effect_id, observed)
        assert restarted.intent(effect, now=now).outcome == "unknown"


def test_recovery_prevents_new_candidate_effects(tmp_path: Path) -> None:
    journal = ExecutorJournal(tmp_path / "core", core_plan())
    now = datetime.now(UTC)
    state = journal.activation(activation(journal, now), now=now)
    assert state.readiness_deadline is not None
    journal.capture_recovery(now=now + timedelta(seconds=1))
    effect = CoreExecutorEffect(
        job_id=state.job_id,
        generation=state.generation,
        plan_sha256=state.plan_sha256,
        effect_id="start-api-candidate",
        action="start_candidate",
        service="coire-api",
        container_id="c" * 64,
        image_id=journal.plan.target.manifest.services[0].image_id,
        captured_at=now,
        deadline=state.readiness_deadline,
    )
    with pytest.raises(UpgradeForbidden):
        journal.intent(effect, now=now + timedelta(seconds=2))
    assert not journal.effects.path(effect.effect_id + ".json").exists()


def test_dispatch_marks_uncertainty_before_transport_and_forbids_blind_replay(
    tmp_path: Path,
) -> None:
    from coire_core.deploy import host_boot_identity
    from coire_core.models.upgrades import DeploymentOwnership
    from coire_upgrade_executor.ownership import executor_ownership

    plan = core_plan()
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "journal", plan)
    state = journal.activation(activation(journal, now), now=now)
    assert state.readiness_deadline is not None
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="executor-source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )
    effect = CoreExecutorEffect(
        job_id=state.job_id,
        generation=state.generation,
        plan_sha256=state.plan_sha256,
        effect_id="stop-api-source",
        action="stop_source",
        service="coire-api",
        container_id="c" * 64,
        image_id=journal.plan.source.manifest.services[0].image_id,
        captured_at=now,
        deadline=state.readiness_deadline,
    )
    journal.intent(effect, now=now)
    with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
        with pytest.raises(UpgradeConflict):
            journal.dispatch_intent(effect.effect_id, authority, now=now)
        journal.capture_outage(now=now)
        dispatched = journal.dispatch_intent(effect.effect_id, authority, now=now)
        assert dispatched.outcome == "unknown"
        assert journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect) == dispatched
        with pytest.raises(UpgradeConflict):
            journal.dispatch_intent(effect.effect_id, authority, now=now)


@pytest.mark.parametrize(
    "fault",
    [None, "running", "exit", "created", "grant", "schema", "image", "name", "stale", "future"],
)
def test_migration_effect_requires_exact_terminal_container_and_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    from coire_core.models.upgrades import UpgradeDockerMigration, UpgradeMigrationExecution
    from coire_upgrade_executor import journal as journal_module

    plan = core_plan(schema_changed=True)
    assert plan.initial_schema_head is not None
    assert plan.initial_schema_sha256 is not None
    assert plan.schema_head_after is not None
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "migration", plan, prepared_root=tmp_path / "prepared")
    runtime = activation(journal, now)
    consumed = UpgradeActivationConsume.model_validate(
        runtime.model_dump()
        | {
            "grant": runtime.grant.model_dump() | {"boundary": "migration"},
            "intent": runtime.intent.model_dump() | {"phase": UpgradePhase.MIGRATING},
        }
    )
    journal.activation(consumed, now=now)
    image = "sha256:" + "e" * 64
    configuration = {
        "image": image,
        "user": "65532:65532",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "no",
        "pids_limit": 128,
        "mem_limit": "512m",
        "networks": {"coire-db": {}, "coire-telemetry": {}},
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    monkeypatch.setattr(
        journal_module,
        "read_prepared_compose",
        lambda *_: {"services": {"coire-migrate": configuration}},
    )
    effect = CoreExecutorEffect(
        job_id=plan.operation.job_id,
        generation=1,
        plan_sha256=plan.authority_sha256(),
        effect_id="migration",
        action="migration",
        image_id=image,
        captured_at=now,
        deadline=now + timedelta(seconds=120),
    )
    journal.intent(effect, now=now)
    proof = UpgradeMigrationExecution(
        job_id=effect.job_id,
        generation=1,
        plan_sha256=effect.plan_sha256,
        grant_id=uuid4() if fault == "grant" else consumed.grant.grant_id,
        state="complete",
        captured_at=now,
        deadline_at=effect.deadline,
        schema_before_head=plan.initial_schema_head,
        schema_before_sha256="f" * 64 if fault == "schema" else plan.initial_schema_sha256,
        expected_schema_head=plan.schema_head_after,
        schema_after_head=plan.schema_head_after,
        schema_after_sha256="c" * 64,
        exit_code=0,
        completed_at=now + timedelta(seconds=1) if fault == "future" else now,
    )
    observed = UpgradeDockerMigration(
        container_id="d" * 64,
        name="foreign"
        if fault == "name"
        else f"coire-upgrade-{effect.job_id.lower()}-coire-migrate",
        project="coire",
        image_id="sha256:" + "f" * 64 if fault == "image" else image,
        observed_at=now - timedelta(seconds=6) if fault == "stale" else now,
        running=fault == "running",
        status="created" if fault == "created" else "exited",
        exit_code=1 if fault == "exit" else 0,
        network_ids={"coire-db": "b" * 64},
    )
    if fault:
        with pytest.raises(UpgradeConflict):
            journal.complete_migration(effect.effect_id, observed, proof, now=now)
        assert not journal.store.path("migration-proof.json").exists()
    else:
        completed = journal.complete_migration(effect.effect_id, observed, proof, now=now)
        assert (
            completed.outcome == "complete"
            and completed.observed_container_id == observed.container_id
        )
        assert journal.complete_migration(effect.effect_id, observed, proof, now=now) == completed
