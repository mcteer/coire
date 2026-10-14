"""Executor transport validates authority, bounded replies and durable acknowledgment cursors."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from upgrade_bootstrap_fixtures import bootstrap_heartbeat as heartbeat_fixture
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreUpgradeInventory,
    UpgradeActivationConsume,
    UpgradeActivationGrant,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeReceiptBatch,
    UpgradeSchemaObservation,
    UpgradeSmokeResult,
)
from coire_core.settings import Settings
from coire_upgrade_executor.client import ExecutorClient


@pytest.mark.parametrize("fault", [None, "changed", "malformed", "unavailable", "stale"])
async def test_bootstrap_transport_does_not_renew_or_change_original_proof(
    fault: str | None,
) -> None:
    body = heartbeat_fixture()
    if fault == "stale":
        body = body.model_copy(
            update={
                "physical": body.physical.model_copy(
                    update={"observed_at": datetime.now(UTC) - timedelta(seconds=6)}
                )
            }
        )
    calls = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert (
            request.method == "POST"
            and request.url.path == "/api/v1/internal/upgrades/bootstrap/heartbeat"
        )
        assert json.loads(request.content) == body.model_dump(mode="json")
        value = body.model_dump(mode="json")
        if fault == "changed":
            value["ownership"]["generation"] += 1
        return httpx.Response(
            503 if fault == "unavailable" else 200, json={} if fault == "malformed" else value
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await client.bootstrap_heartbeat(body)
            if fault == "stale":
                assert not calls
        else:
            assert await client.bootstrap_heartbeat(body) == body
            assert client.plan is None


@pytest.mark.parametrize("fault", [None, "plan", "schema", "malformed", "unavailable"])
async def test_not_started_fence_transport_preserves_accepted_source(fault: str | None) -> None:
    from coire_core.models.upgrades import UpgradeMigrationNotStarted

    plan = core_plan(schema_changed=True)
    assert plan.initial_schema_head is not None and plan.initial_schema_sha256 is not None
    proof = UpgradeMigrationNotStarted(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256="f" * 64 if fault == "plan" else plan.authority_sha256(),
        grant_id=uuid4(),
        schema_head=plan.initial_schema_head,
        schema_sha256="0" * 64 if fault == "schema" else plan.initial_schema_sha256,
        observed_at=datetime.now(UTC),
    )

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert (
            request.url.path
            == f"/api/v1/internal/upgrades/{plan.operation.job_id}/migration-not-started"
        )
        assert not request.content
        return httpx.Response(
            503 if fault == "unavailable" else 200,
            json={} if fault == "malformed" else proof.model_dump(mode="json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        client.bind(plan)
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await client.fence_migration_not_started()
        else:
            assert await client.fence_migration_not_started() == proof


@pytest.mark.parametrize("fault", [None, "runtime", "stale", "future", "duration", "role"])
@pytest.mark.parametrize("restored", [False, True])
async def test_gateway_smoke_binds_fresh_selected_release(
    fault: str | None, restored: bool
) -> None:
    plan = core_plan()
    release = plan.source if restored else plan.target
    now = datetime.now(UTC)
    finished = now + timedelta(seconds=1 if fault == "future" else -6 if fault == "stale" else 0)
    result = UpgradeSmokeResult(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        check=("gateway" if restored else "source")
        if fault == "role"
        else "source"
        if restored
        else "gateway",
        runtime_sha256="f" * 64 if fault == "runtime" else release.manifest_sha256,
        model_id=plan.model_id,
        model_variant_id=plan.model_variant_id,
        model_manifest_sha256=plan.model_manifest_sha256,
        generated_tokens=10,
        passed=True,
        started_at=finished - timedelta(seconds=121 if fault == "duration" else 1),
        finished_at=finished,
        output_sha256="e" * 64,
    )

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path.endswith("/smoke")
        assert 0 < request.extensions["timeout"]["read"] <= 120
        assert json.loads(request.content) == {
            "generation": plan.operation.generation,
            "check": "source" if restored else "gateway",
        }
        return httpx.Response(200, json=result.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        client.bind(plan)
        if fault:
            with pytest.raises(UpgradeConflict):
                await client.smoke(restored=restored, deadline=now + timedelta(seconds=120))
        else:
            assert (
                await client.smoke(restored=restored, deadline=now + timedelta(seconds=120))
                == result
            )


async def test_expired_smoke_budget_does_not_send_a_gateway_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        client.bind(core_plan())
        send = AsyncMock()
        monkeypatch.setattr(http, "send", send)
        with pytest.raises(UpgradeForbidden, match="deadline"):
            await client.smoke(deadline=datetime.now(UTC) - timedelta(seconds=1))
        send.assert_not_awaited()


def settings() -> Settings:
    return Settings(upgrade_executor_token=SecretStr("coire_upgrade_private-test"))


@pytest.mark.parametrize("fault", [None, "job", "generation", "stale", "future", "malformed"])
async def test_schema_observation_requires_fresh_exact_operation(fault: str | None) -> None:
    plan = core_plan()
    observed = UpgradeSchemaObservation(
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FA0" if fault == "job" else plan.operation.job_id,
        generation=2 if fault == "generation" else plan.operation.generation,
        schema_head="0034_upgrades",
        schema_sha256="d" * 64,
        observed_at=datetime.now(UTC)
        + timedelta(seconds=1 if fault == "future" else -6 if fault == "stale" else 0),
    )

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path.endswith("/schema")
        body = observed.model_dump(mode="json")
        if fault == "malformed":
            body["private_definitions"] = "must be refused"
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        client.bind(plan)
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await client.schema()
        else:
            assert await client.schema() == observed


async def test_claim_binds_exact_source_and_cannot_switch_plan() -> None:
    plan = core_plan()
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST" and request.url.path == "/api/v1/internal/upgrades/claim"
        assert request.headers["authorization"] == "Bearer coire_upgrade_private-test"
        return httpx.Response(200, json=plan.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        assert await client.claim(plan.operation.source_sha256) == plan
        with pytest.raises(UpgradeConflict):
            client.bind(core_plan())
        assert len(seen) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/chat/completions"),
        ("GET", "http://foreign/private"),
        ("POST", "/api/v1/internal/upgrades/another/receipts"),
        ("DELETE", "/api/v1/internal/upgrades/claim"),
    ],
)
async def test_unscoped_operations_never_reach_core(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        client.bind(core_plan())
        send = AsyncMock()
        monkeypatch.setattr(http, "send", send)
        with pytest.raises(UpgradeForbidden):
            await client._request(method, path, None)
        send.assert_not_awaited()


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "job",
        "generation",
        "cursor",
        "redirect",
        "duplicate",
        "oversized",
        "nonfinite",
        "credential",
    ],
)
async def test_receipts_require_durable_matching_acknowledgment(fault: str | None) -> None:
    plan = core_plan()
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=1,
        sequence=2,
        effect_id="fencing",
        phase=UpgradePhase.FENCING,
        occurred_at=datetime.now(UTC),
        outcome="complete",
    )

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path.endswith("/receipts")
        if fault == "redirect":
            return httpx.Response(302, headers={"Location": "http://foreign/private"})
        if fault == "duplicate":
            return httpx.Response(200, content=b'{"sequence":2,"sequence":3}')
        if fault == "oversized":
            return httpx.Response(200, content=b"private-data" * 100000)
        if fault == "nonfinite":
            return httpx.Response(200, content=b'{"sequence":NaN}')
        if fault == "credential":
            return httpx.Response(403, json={"private": "credential must not escape"})
        return httpx.Response(
            200,
            json={
                "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FA0" if fault == "job" else receipt.job_id,
                "generation": 2 if fault == "generation" else 1,
                "sequence": 1 if fault == "cursor" else 2,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        client.bind(plan)
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable, UpgradeForbidden)) as error:
                await client.receipts(UpgradeReceiptBatch(receipts=(receipt,)))
            assert "private-data" not in str(error.value) and "credential must" not in str(
                error.value
            )
        else:
            assert (await client.receipts(UpgradeReceiptBatch(receipts=(receipt,)))).sequence == 2


@pytest.mark.parametrize("fault", [None, "expired", "long", "boundary"])
async def test_activation_grant_and_consumption_bind_original_boundary(fault: str | None) -> None:
    plan = core_plan()
    grant = UpgradeActivationGrant(
        job_id=plan.operation.job_id,
        generation=1,
        boundary="migration" if fault == "boundary" else "runtime",
        grant_id=uuid4(),
        expires_at=datetime.now(UTC)
        + timedelta(seconds=-1 if fault == "expired" else 180 if fault == "long" else 30),
    )
    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/ready-to-activate"):
            return httpx.Response(200, json=grant.model_dump(mode="json"))
        assert request.url.path.endswith("/consume-activation")
        return httpx.Response(200, json={"job_id": grant.job_id, "generation": 1, "sequence": 3})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        client.bind(plan)
        if fault:
            with pytest.raises(UpgradeConflict):
                await client.grant()
            assert len(calls) == 1
            return
        accepted = await client.grant()
        intent = UpgradeReceipt(
            job_id=grant.job_id,
            generation=1,
            sequence=3,
            effect_id="runtime",
            phase=UpgradePhase.ACTIVATING,
            occurred_at=datetime.now(UTC),
            outcome="intent",
            observed_sha256=plan.operation.source_sha256,
        )
        ack = await client.consume(UpgradeActivationConsume(grant=accepted, intent=intent))
        assert ack.sequence == 3
        assert len(calls) == 2


@pytest.mark.parametrize(
    "fault", [None, "job", "generation", "project", "source", "stale", "future"]
)
async def test_core_inventory_rejects_stale_or_foreign_drain_observations(
    fault: str | None,
) -> None:
    plan = core_plan()
    snapshot = CoreUpgradeInventory(
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FA0" if fault == "job" else plan.operation.job_id,
        generation=2 if fault == "generation" else plan.operation.generation,
        project="other" if fault == "project" else "coire",
        release_sha256="f" * 64 if fault == "source" else plan.operation.source_sha256,
        observed_at=datetime.now(UTC)
        + timedelta(seconds=1 if fault == "future" else -6 if fault == "stale" else 0),
        owners=(),
    )

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/internal/upgrades/{plan.operation.job_id}/inventory"
        return httpx.Response(200, json=snapshot.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        client.bind(plan)
        if fault:
            with pytest.raises(UpgradeConflict):
                await client.inventory()
        else:
            assert await client.inventory() == snapshot
            # A complete transport response alone cannot assert that every owner is idle.
            assert not snapshot.idle(frozenset({"requests", "scheduler"}))


@pytest.mark.parametrize("fault", [None, "idle", "changed", "expired", "malformed"])
async def test_handover_claim_never_binds_application_plan(fault: str | None) -> None:
    from test_handover_creation_journal import preparation

    value = preparation()
    seed = value.incumbent
    body = value.model_dump(mode="json")
    if fault == "idle":
        body = {"idle": True}
    elif fault == "changed":
        body["incumbent"]["executor_runtime_id"] = "foreign"
    elif fault == "expired":
        old = datetime.now(UTC) - timedelta(seconds=121)
        body["prepared_at"] = old.isoformat()
        body["deadline_at"] = (old + timedelta(seconds=120)).isoformat()
    elif fault == "malformed":
        body = {"command": "docker exec"}

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/internal/upgrades/bootstrap/handover-claim"
        assert json.loads(request.content) == seed.model_dump(mode="json")
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        if fault in {"changed", "expired", "malformed"}:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await client.claim_handover(seed)
        else:
            assert await client.claim_handover(seed) == (None if fault == "idle" else value)
        assert client.plan is None


@pytest.mark.parametrize(
    "fault", [None, "handover", "generation", "seed", "proof", "future", "extra"]
)
async def test_commit_transport_preserves_original_selection_decision(fault: str | None) -> None:
    from test_handover_creation_journal import preparation

    from coire_core.models.upgrades import (
        BootstrapStabilityObservation,
        CoreHandoverCommitIntent,
        CoreHandoverTransferIntent,
    )

    value = preparation()
    now = datetime.now(UTC)
    proof = BootstrapStabilityObservation(
        generation=value.generation,
        incumbent_id=value.incumbent.executor_runtime_id,
        incumbent_container_id=value.incumbent.executor_container_id,
        incumbent_image_id=value.incumbent.executor_image_id,
        candidate_id=value.candidate_runtime_id,
        candidate_container_id="a" * 64,
        candidate_image_id=value.candidate_image_id,
        kernel_boot_identity="isolated-kernel",
        candidate_started_at=now,
        observed_at=now,
        incumbent_healthy=True,
        candidate_healthy=True,
        exclusive_writer_verified=True,
    )
    transfer = CoreHandoverTransferIntent(
        handover_id=uuid4(),
        intent_id=uuid4(),
        preparation_sha256=value.sha256(),
        candidate_seed_sha256="b" * 64,
        passive_proof_sha256="c" * 64,
        generation=value.generation,
        consumed_at=value.prepared_at,
        deadline_at=value.prepared_at + timedelta(seconds=30),
    )
    decision = CoreHandoverCommitIntent(
        handover_id=transfer.handover_id,
        decision_id=uuid4(),
        preparation_sha256=value.sha256(),
        candidate_seed_sha256=transfer.candidate_seed_sha256,
        stability_sha256=proof.sha256(),
        generation=value.generation,
        decided_at=now,
    )
    body = decision.model_dump(mode="json")
    if fault == "handover":
        body["handover_id"] = str(uuid4())
    elif fault == "generation":
        body["generation"] += 1
    elif fault == "seed":
        body["candidate_seed_sha256"] = "e" * 64
    elif fault == "proof":
        body["stability_sha256"] = "f" * 64
    elif fault == "future":
        body["decided_at"] = (now + timedelta(seconds=1)).isoformat()
    elif fault == "extra":
        body["command"] = "unbounded"

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/internal/upgrades/bootstrap/handover-commit"
        assert json.loads(request.content) == {
            "preparation_sha256": value.sha256(),
            "stability": proof.model_dump(mode="json"),
        }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await client.decide_handover_commit(value, transfer, proof)
        else:
            assert await client.decide_handover_commit(value, transfer, proof) == decision
        assert client.plan is None


@pytest.mark.parametrize("fault", [None, "phase", "generation", "project", "deadline", "extra"])
async def test_handover_status_never_follows_a_different_preparation(fault: str | None) -> None:
    from test_handover_creation_journal import preparation

    from coire_core.models.upgrades import CoreHandoverSummary

    value = preparation()
    summary = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=value.generation,
        phase="committed",
        preparation_sha256=value.sha256(),
        candidate_release_id=value.candidate_release.release_id,
        incumbent_image_id=value.incumbent.executor_image_id,
        candidate_image_id=value.candidate_image_id,
        prepared_at=value.prepared_at,
        deadline_at=value.deadline_at,
    )
    body = summary.model_dump(mode="json")
    if fault == "phase":
        body["phase"] = "invented"
    elif fault == "generation":
        body["generation"] += 1
    elif fault == "project":
        body["project"] = "other"
    elif fault == "deadline":
        body["deadline_at"] = (value.deadline_at + timedelta(seconds=1)).isoformat()
    elif fault == "extra":
        body["command"] = "unbounded"
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET" and not request.content
        assert request.url.path == "/api/v1/internal/upgrades/bootstrap/handover/" + value.sha256()
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await client.handover_status(value)
        else:
            assert await client.handover_status(value) == summary
            with pytest.raises(UpgradeConflict):
                await client.handover_status(
                    value.model_copy(update={"candidate_runtime_id": "foreign"})
                )
            assert len(requests) == 1
        assert client.plan is None


@pytest.mark.parametrize("fault", [None, "generation", "proof", "deadline", "future"])
async def test_transfer_transport_binds_captured_intent_without_application_authority(
    fault: str | None,
) -> None:
    from test_handover_creation_journal import preparation

    from coire_core.models.upgrades import CoreHandoverTransferIntent, ExecutorPassiveReadiness

    value = preparation()
    proof = ExecutorPassiveReadiness(
        preparation_sha256=value.sha256(),
        candidate_seed_sha256="b" * 64,
        generation=value.generation,
        candidate_runtime_id=value.candidate_runtime_id,
        candidate_container_id="a" * 64,
        candidate_image_id=value.candidate_image_id,
        incumbent_runtime_id=value.incumbent.executor_runtime_id,
        incumbent_container_id=value.incumbent.executor_container_id,
        observed_at=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    intent = CoreHandoverTransferIntent(
        handover_id=uuid4(),
        intent_id=uuid4(),
        preparation_sha256=value.sha256(),
        candidate_seed_sha256=proof.candidate_seed_sha256,
        passive_proof_sha256=proof.sha256(),
        generation=value.generation,
        consumed_at=now,
        deadline_at=now + timedelta(seconds=30),
    )
    body = intent.model_dump(mode="json")
    if fault == "generation":
        body["generation"] += 1
    elif fault == "proof":
        body["passive_proof_sha256"] = "c" * 64
    elif fault == "deadline":
        body["deadline_at"] = (value.deadline_at + timedelta(seconds=1)).isoformat()
    elif fault == "future":
        body["consumed_at"] = (now + timedelta(seconds=1)).isoformat()
        body["deadline_at"] = (now + timedelta(seconds=31)).isoformat()

    def transport(request: httpx.Request) -> httpx.Response:
        assert (
            request.method == "POST"
            and request.url.path == "/api/v1/internal/upgrades/bootstrap/handover-transfer"
        )
        assert json.loads(request.content) == {
            "preparation_sha256": value.sha256(),
            "passive_proof_sha256": proof.sha256(),
        }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await client.consume_handover_transfer(value, proof)
        else:
            assert await client.consume_handover_transfer(value, proof) == intent
        assert client.plan is None


@pytest.mark.parametrize("fault", [None, "foreign", "malformed"])
async def test_expired_recovery_metadata_never_renews_or_grants_passive_creation(
    fault: str | None,
) -> None:
    from test_handover_creation_journal import preparation

    original = preparation()
    expired = original.model_copy(
        update={
            "prepared_at": original.prepared_at - timedelta(seconds=241),
            "deadline_at": original.deadline_at - timedelta(seconds=241),
        }
    )
    settings = Settings(
        upgrades_enabled=True, upgrade_executor_token=SecretStr("coire_upgrade_fixture")
    )
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "POST"
        assert request.url.path.endswith("/bootstrap/handover-recovery-claim")
        assert json.loads(request.content) == original.incumbent.model_dump(mode="json")
        result = expired.model_dump(mode="json")
        if fault == "foreign":
            result["incumbent"]["executor_container_id"] = "f" * 64
        elif fault == "malformed":
            result["renewed_deadline"] = datetime.now(UTC).isoformat()
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as http:
        client = ExecutorClient(settings, client=http)
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await client.claim_handover_recovery(original.incumbent)
        else:
            assert await client.claim_handover_recovery(original.incumbent) == expired
            assert expired.deadline_at < datetime.now(UTC)
        assert client.plan is None
    assert len(paths) == 1


@pytest.mark.parametrize("expired", [False, True])
async def test_handover_recovery_retains_original_deadline_and_five_second_transport(
    expired: bool,
) -> None:
    from test_handover_creation_journal import preparation

    from coire_core.models.upgrades import (
        CoreHandoverRecoveryIntent,
        CoreHandoverRecoveryRequest,
        CoreHandoverSummary,
    )

    value = preparation()
    detected = datetime.now(UTC) - timedelta(seconds=301 if expired else 0)
    intent = CoreHandoverRecoveryIntent(
        preparation_sha256=value.sha256(),
        candidate_seed_sha256="d" * 64,
        generation=value.generation,
        incumbent_container_id=value.incumbent.executor_container_id,
        candidate_container_id="a" * 64,
        detected_at=detected,
        deadline_at=detected + timedelta(seconds=300),
    )
    request = CoreHandoverRecoveryRequest(recovery=intent)
    summary = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=value.generation,
        phase="restoring",
        preparation_sha256=value.sha256(),
        candidate_release_id=value.candidate_release.release_id,
        incumbent_image_id=value.incumbent.executor_image_id,
        candidate_image_id=value.candidate_image_id,
        prepared_at=value.prepared_at,
        deadline_at=value.deadline_at,
    )
    calls = []

    def transport(wire: httpx.Request) -> httpx.Response:
        calls.append(wire)
        assert wire.url.path == "/api/v1/internal/upgrades/bootstrap/handover-restoring"
        assert json.loads(wire.content) == request.model_dump(mode="json")
        assert 0 < wire.extensions["timeout"]["read"] <= 5
        return httpx.Response(200, json=summary.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(settings(), client=http)
        if expired:
            with pytest.raises(UpgradeForbidden, match="deadline"):
                await client.begin_handover_restoration(value, request)
            assert not calls
        else:
            assert await client.begin_handover_restoration(value, request) == summary
            assert len(calls) == 1
            with pytest.raises(UpgradeForbidden):
                await client._request(
                    "POST",
                    "/api/v1/internal/upgrades/bootstrap/handover-restoring",
                    request,
                    deadline=intent.deadline_at + timedelta(seconds=1),
                )
            assert len(calls) == 1
