"""Internal routes grant only the restricted executor accepted-plan transport."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from upgrade_bootstrap_fixtures import bootstrap_heartbeat as heartbeat_fixture
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.routes import internal_upgrades
from coire_core.errors import CoireError
from coire_core.models.upgrades import (
    CoreUpgradeInventory,
    UpgradeClaimIdle,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("allowed", [False, True])
def test_bootstrap_heartbeat_is_restricted_and_retains_the_typed_original_proof(
    monkeypatch, allowed
):
    from coire_api.upgrades import bootstrap_heartbeat as service

    body = heartbeat_fixture()
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, _ = client(monkeypatch, principal)
    retained = AsyncMock(return_value=body)
    monkeypatch.setattr(service, "retain_bootstrap_heartbeat", retained)
    response = api.post(
        "/api/v1/internal/upgrades/bootstrap/heartbeat", json=body.model_dump(mode="json")
    )
    assert response.status_code == (200 if allowed else 403)
    if not allowed:
        retained.assert_not_awaited()
        return
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == body.model_dump(mode="json")
    retained.assert_awaited_once()


@pytest.mark.parametrize("allowed", [False, True])
def test_not_started_fence_is_executor_scoped_and_not_a_migration_success(monkeypatch, allowed):
    from datetime import UTC, datetime
    from uuid import uuid4

    from coire_api.upgrades import migration
    from coire_core.models.upgrades import UpgradeMigrationNotStarted

    plan = core_plan(schema_changed=True)
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, session = client(monkeypatch, principal)
    scoped = AsyncMock(return_value=(object(), plan))
    monkeypatch.setattr(internal_upgrades.executor, "scoped_operation", scoped)
    proof = UpgradeMigrationNotStarted(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        grant_id=uuid4(),
        schema_head=plan.initial_schema_head,
        schema_sha256=plan.initial_schema_sha256,
        observed_at=datetime.now(UTC),
    )
    fenced = AsyncMock(return_value=proof)
    monkeypatch.setattr(migration, "fence_not_started", fenced)
    response = api.post(f"/api/v1/internal/upgrades/{plan.operation.job_id}/migration-not-started")
    assert response.status_code == (200 if allowed else 403)
    if not allowed:
        scoped.assert_not_awaited()
        fenced.assert_not_awaited()
        return
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == proof.model_dump(mode="json")
    assert "exit_code" not in response.json()
    fenced.assert_awaited_once_with(session, plan, principal, api.app.state.settings)


@pytest.mark.parametrize("allowed", [False, True])
def test_schema_observation_requires_executor_scope_before_reading_database(monkeypatch, allowed):
    from coire_api.upgrades import schema_identity

    value = core_plan()
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, session = client(monkeypatch, principal)
    scoped = AsyncMock(return_value=(object(), value))
    observed = AsyncMock(return_value=("0034_upgrades", "d" * 64))
    monkeypatch.setattr(internal_upgrades.executor, "scoped_operation", scoped)
    monkeypatch.setattr(schema_identity, "inspect_schema", observed)
    response = api.get(f"/api/v1/internal/upgrades/{value.operation.job_id}/schema")
    if not allowed:
        assert response.status_code == 403
        scoped.assert_not_awaited()
        observed.assert_not_awaited()
        return
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["schema_head"] == "0034_upgrades"
    assert response.json()["schema_sha256"] == "d" * 64
    assert response.json()["generation"] == value.operation.generation
    scoped.assert_awaited_once_with(
        session, value.operation.job_id, principal, api.app.state.settings
    )
    observed.assert_awaited_once_with(session)
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


def client(monkeypatch: pytest.MonkeyPatch, principal: Principal, *, enabled: bool = True):
    app = FastAPI()
    app.state.settings = Settings(upgrades_enabled=enabled)
    app.state.upgrade_admissions = object()
    app.include_router(internal_upgrades.router)
    app.dependency_overrides[require_principal] = lambda: principal

    async def problem(request, error):
        return JSONResponse(error.to_problem().model_dump(mode="json"), status_code=error.status)

    app.add_exception_handler(CoireError, problem)
    session = AsyncMock()

    @asynccontextmanager
    async def sessions():
        yield session

    monkeypatch.setattr(internal_upgrades, "session_scope", sessions)
    return TestClient(app), session


def test_core_inventory_is_scoped_to_the_claimed_plan_and_never_claims_or_mutates(monkeypatch):
    from datetime import UTC, datetime

    from coire_api.upgrades import inventory as inventory_service

    value = core_plan()
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR,
        subject="coire-upgrade-executor",
        scopes=frozenset({"upgrades:execute"}),
    )
    api, session = client(monkeypatch, principal)
    scoped = AsyncMock(return_value=(object(), value))
    monkeypatch.setattr(internal_upgrades.executor, "scoped_operation", scoped)
    snapshot = CoreUpgradeInventory(
        job_id=value.operation.job_id,
        generation=value.operation.generation,
        project="coire",
        release_sha256=value.operation.source_sha256,
        observed_at=datetime.now(UTC),
        owners=(),
    )
    inventory = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(inventory_service, "core_inventory", inventory)
    response = api.get(f"/api/v1/internal/upgrades/{value.operation.job_id}/inventory")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == snapshot.model_dump(mode="json")
    scoped.assert_awaited_once_with(
        session, value.operation.job_id, principal, api.app.state.settings
    )
    inventory.assert_awaited_once_with(
        session, api.app.state.settings, value, api.app.state.upgrade_admissions
    )


@pytest.mark.parametrize(
    "kind,subject,scopes",
    [
        (PrincipalKind.ADMIN, "admin", {"admin"}),
        (PrincipalKind.ANONYMOUS, None, set()),
        (PrincipalKind.UPGRADE_EXECUTOR, "another", {"upgrades:execute"}),
        (PrincipalKind.UPGRADE_EXECUTOR, "coire-upgrade-executor", {"admin", "upgrades:execute"}),
    ],
)
def test_only_exact_executor_identity_and_scope_can_claim(monkeypatch, kind, subject, scopes):
    principal = Principal(kind=kind, subject=subject, scopes=frozenset(scopes))
    api, session = client(monkeypatch, principal)
    claimed = AsyncMock(return_value=UpgradeClaimIdle())
    monkeypatch.setattr(internal_upgrades.executor, "claim", claimed)
    response = api.post(
        "/api/v1/internal/upgrades/claim",
        json={
            "executor_id": "coire-upgrade-executor",
            "project": "coire",
            "observed_source_sha256": "a" * 64,
        },
    )
    assert response.status_code == 403
    claimed.assert_not_awaited()
    session.scalar.assert_not_awaited()


def test_claim_is_bounded_disabled_and_never_accepts_docker_commands(monkeypatch):
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR,
        subject="coire-upgrade-executor",
        scopes=frozenset({"upgrades:execute"}),
    )
    api, _ = client(monkeypatch, principal)
    claimed = AsyncMock(return_value=UpgradeClaimIdle())
    monkeypatch.setattr(internal_upgrades.executor, "claim", claimed)
    body = {
        "executor_id": principal.subject,
        "project": "coire",
        "observed_source_sha256": "a" * 64,
    }
    response = api.post("/api/v1/internal/upgrades/claim", json=body)
    assert response.status_code == 200 and response.json() == {"idle": True}
    assert response.headers["cache-control"] == "no-store"
    claimed.reset_mock()
    assert (
        api.post(
            "/api/v1/internal/upgrades/claim", json=body | {"docker_path": "/exec/anything/start"}
        ).status_code
        == 422
    )
    claimed.assert_not_awaited()
    disabled, _ = client(monkeypatch, principal, enabled=False)
    assert disabled.post("/api/v1/internal/upgrades/claim", json=body).status_code == 503
    claimed.assert_not_awaited()


def test_receipt_batch_cannot_change_job_or_generation(monkeypatch):
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR,
        subject="coire-upgrade-executor",
        scopes=frozenset({"upgrades:execute"}),
    )
    api, _ = client(monkeypatch, principal)
    plan = core_plan()
    row = SimpleNamespace(payload=plan.operation.model_dump(mode="json"))
    scoped = AsyncMock(return_value=(row, plan))
    ingest = AsyncMock()
    monkeypatch.setattr(internal_upgrades.executor, "scoped_operation", scoped)
    monkeypatch.setattr(internal_upgrades, "ingest_receipt", ingest)
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=2,
        sequence=1,
        effect_id="fencing",
        phase=UpgradePhase.FENCING,
        occurred_at=plan.operation.accepted_at,
        outcome="complete",
    )
    response = api.post(
        f"/api/v1/internal/upgrades/{plan.operation.job_id}/receipts",
        json={"receipts": [receipt.model_dump(mode="json")]},
    )
    assert response.status_code == 409
    ingest.assert_not_awaited()


@pytest.mark.parametrize("allowed", [False, True])
def test_gateway_smoke_is_restricted_and_returns_only_bounded_evidence(monkeypatch, allowed):
    from datetime import UTC, datetime

    from coire_api.upgrades import gateway_smoke
    from coire_core.models.upgrades import UpgradeSmokeResult

    plan = core_plan()
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, _ = client(monkeypatch, principal)
    now = datetime.now(UTC)
    result = UpgradeSmokeResult(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        check="gateway",
        runtime_sha256=plan.target.manifest_sha256,
        model_id=plan.model_id,
        model_variant_id=plan.model_variant_id,
        model_manifest_sha256=plan.model_manifest_sha256,
        generated_tokens=64,
        passed=True,
        started_at=now,
        finished_at=now,
        output_sha256="a" * 64,
    )
    run = AsyncMock(return_value=result)
    monkeypatch.setattr(gateway_smoke, "run_gateway_smoke", run)
    response = api.post(
        f"/api/v1/internal/upgrades/{plan.operation.job_id}/smoke",
        json={"generation": plan.operation.generation, "check": "gateway"},
    )
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == result.model_dump(mode="json")
        run.assert_awaited_once()
    else:
        run.assert_not_awaited()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("recovery", [False, True])
def test_handover_claim_is_restricted_and_does_not_grant_application_authority(
    monkeypatch, allowed, recovery
):
    from coire_api.upgrades import handover_authority

    seed = heartbeat_fixture().seed
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, _ = client(monkeypatch, principal)
    claimed = AsyncMock(return_value=UpgradeClaimIdle())
    monkeypatch.setattr(
        handover_authority, "recovery_claim_handover" if recovery else "claim_handover", claimed
    )
    suffix = "handover-recovery-claim" if recovery else "handover-claim"
    response = api.post(
        "/api/v1/internal/upgrades/bootstrap/" + suffix, json=seed.model_dump(mode="json")
    )
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.json() == {"idle": True}
        assert response.headers["cache-control"] == "no-store"
        claimed.assert_awaited_once()
    else:
        claimed.assert_not_awaited()


@pytest.mark.parametrize("allowed", [False, True])
def test_transfer_consumption_is_executor_only_and_typed(monkeypatch, allowed):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from coire_api.upgrades import handover_transfer
    from coire_core.models.upgrades import CoreHandoverTransferIntent

    now = datetime.now(UTC)
    intent = CoreHandoverTransferIntent(
        handover_id=uuid4(),
        intent_id=uuid4(),
        preparation_sha256="a" * 64,
        candidate_seed_sha256="b" * 64,
        passive_proof_sha256="c" * 64,
        generation=2,
        consumed_at=now,
        deadline_at=now + timedelta(seconds=30),
    )
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, _ = client(monkeypatch, principal)
    consumed = AsyncMock(return_value=intent)
    monkeypatch.setattr(handover_transfer, "consume_handover_transfer", consumed)
    body = {"preparation_sha256": "a" * 64, "passive_proof_sha256": "c" * 64}
    response = api.post("/api/v1/internal/upgrades/bootstrap/handover-transfer", json=body)
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.json() == intent.model_dump(mode="json")
        assert response.headers["cache-control"] == "no-store"
        assert (
            api.post(
                "/api/v1/internal/upgrades/bootstrap/handover-transfer",
                json=body | {"command": "docker stop"},
            ).status_code
            == 422
        )
        consumed.assert_awaited_once()
    else:
        consumed.assert_not_awaited()


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("phase", ["failed_unchanged", "restoring", "restored", "committed"])
def test_handover_terminal_acknowledgment_is_executor_only_and_typed(monkeypatch, allowed, phase):
    from datetime import timedelta
    from uuid import uuid4

    from coire_api.upgrades import handover_completion, handover_restoration, handover_unchanged
    from coire_core.models.upgrades import CoreHandoverSummary

    heartbeat = heartbeat_fixture()
    now = heartbeat.physical.observed_at
    summary = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=2,
        phase=phase,
        preparation_sha256="a" * 64,
        candidate_release_id=uuid4(),
        incumbent_image_id=heartbeat.seed.executor_image_id,
        candidate_image_id="sha256:" + "f" * 64,
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, _ = client(monkeypatch, principal)
    acknowledged = AsyncMock(return_value=summary)
    if phase == "failed_unchanged":
        monkeypatch.setattr(handover_unchanged, "acknowledge_unchanged", acknowledged)
    elif phase == "restoring":
        monkeypatch.setattr(handover_restoration, "begin_restoration", acknowledged)
    elif phase == "restored":
        monkeypatch.setattr(handover_restoration, "acknowledge_restoration", acknowledged)
    else:
        monkeypatch.setattr(handover_completion, "acknowledge_handover_commit", acknowledged)
    body = {
        "preparation_sha256": "a" * 64,
        "incumbent": heartbeat.model_dump(mode="json"),
        "candidate": {
            "preparation_sha256": "a" * 64,
            "candidate_seed_sha256": "b" * 64,
            "candidate_container_id": "c" * 64,
            "generation": 2,
            "observed_at": now.isoformat(),
            "running": False,
        },
    }
    if phase == "committed":
        body = {
            "preparation_sha256": "a" * 64,
            "candidate": heartbeat.model_dump(mode="json"),
            "incumbent": {
                "preparation_sha256": "a" * 64,
                "incumbent_runtime_id": "retained-incumbent",
                "incumbent_container_id": "c" * 64,
                "incumbent_image_id": "sha256:" + "f" * 64,
                "incumbent_started_at": now.isoformat(),
                "observed_at": now.isoformat(),
                "incumbent_healthy": True,
            },
        }
    elif phase == "failed_unchanged":
        body = {"preparation_sha256": "a" * 64, "incumbent": heartbeat.model_dump(mode="json")}
    elif phase == "restoring":
        body = {
            "recovery": {
                "preparation_sha256": "a" * 64,
                "candidate_seed_sha256": "b" * 64,
                "generation": 2,
                "incumbent_container_id": "c" * 64,
                "candidate_container_id": "d" * 64,
                "detected_at": now.isoformat(),
                "deadline_at": (now + timedelta(seconds=300)).isoformat(),
            }
        }
    path = "/api/v1/internal/upgrades/bootstrap/handover-" + (
        "unchanged" if phase == "failed_unchanged" else phase
    )
    response = api.post(path, json=body)
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.json() == summary.model_dump(mode="json")
        assert response.headers["cache-control"] == "no-store"
        assert api.post(path, json=body | {"command": "restore"}).status_code == 422
        if phase not in {"restoring", "failed_unchanged"}:
            assert (
                api.post(
                    path, json=body | {"candidate": body["candidate"] | {"running": True}}
                ).status_code
                == 422
            )
        elif phase == "restoring":
            assert (
                api.post(
                    path,
                    json={
                        "recovery": body["recovery"]
                        | {"deadline_at": (now + timedelta(seconds=301)).isoformat()}
                    },
                ).status_code
                == 422
            )
        acknowledged.assert_awaited_once()
    else:
        acknowledged.assert_not_awaited()


@pytest.mark.parametrize("allowed", [False, True])
def test_commit_decision_is_executor_only_and_preserves_original_stability(monkeypatch, allowed):
    from datetime import UTC, datetime
    from uuid import uuid4

    from coire_api.upgrades import handover_commit
    from coire_core.models.upgrades import BootstrapStabilityObservation, CoreHandoverCommitIntent

    now = datetime.now(UTC)
    proof = BootstrapStabilityObservation(
        generation=2,
        incumbent_id="incumbent",
        candidate_id="candidate",
        incumbent_container_id="a" * 64,
        candidate_container_id="b" * 64,
        incumbent_image_id="sha256:" + "c" * 64,
        candidate_image_id="sha256:" + "d" * 64,
        kernel_boot_identity="isolated-kernel",
        candidate_started_at=now,
        observed_at=now,
        incumbent_healthy=True,
        candidate_healthy=True,
        exclusive_writer_verified=True,
    )
    decision = CoreHandoverCommitIntent(
        handover_id=uuid4(),
        decision_id=uuid4(),
        preparation_sha256="e" * 64,
        candidate_seed_sha256="f" * 64,
        stability_sha256=proof.sha256(),
        generation=2,
        decided_at=now,
    )
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, _ = client(monkeypatch, principal)
    decided = AsyncMock(return_value=decision)
    monkeypatch.setattr(handover_commit, "decide_handover_commit", decided)
    body = {"preparation_sha256": "e" * 64, "stability": proof.model_dump(mode="json")}
    path = "/api/v1/internal/upgrades/bootstrap/handover-commit"
    response = api.post(path, json=body)
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.json() == decision.model_dump(mode="json")
        assert response.headers["cache-control"] == "no-store"
        assert api.post(path, json=body | {"image": "caller-selected"}).status_code == 422
        decided.assert_awaited_once()
    else:
        decided.assert_not_awaited()


@pytest.mark.parametrize("allowed", [False, True])
def test_handover_status_is_scoped_read_only_and_typed(monkeypatch, allowed):
    from datetime import timedelta
    from uuid import uuid4

    from coire_api.upgrades import handover_authority
    from coire_core.models.upgrades import CoreHandoverSummary

    now = heartbeat_fixture().physical.observed_at
    summary = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=2,
        phase="committed",
        preparation_sha256="a" * 64,
        candidate_release_id=uuid4(),
        incumbent_image_id="sha256:" + "b" * 64,
        candidate_image_id="sha256:" + "c" * 64,
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR if allowed else PrincipalKind.ADMIN,
        subject="coire-upgrade-executor" if allowed else "admin",
        scopes=frozenset({"upgrades:execute"} if allowed else {"admin"}),
    )
    api, session = client(monkeypatch, principal)
    session.scalar.return_value = object()
    monkeypatch.setattr(handover_authority, "handover_summary", lambda row: summary)
    path = "/api/v1/internal/upgrades/bootstrap/handover/" + "a" * 64
    response = api.get(path)
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.json() == summary.model_dump(mode="json")
        assert response.headers["cache-control"] == "no-store"
        statement = str(session.scalar.await_args.args[0])
        assert (
            "upgrade_handovers.project" in statement
            and "upgrade_handovers.preparation_sha256" in statement
        )
        session.add.assert_not_called()
        session.flush.assert_not_awaited()
    else:
        session.scalar.assert_not_awaited()
