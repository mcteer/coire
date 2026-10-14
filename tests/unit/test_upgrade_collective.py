"""Collective wire authority cannot broaden an accepted native pair."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from upgrade_fixtures import plan

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import NodeUpgradeCollectiveRead, NodeUpgradeCollectiveScope
from coire_core.settings import Settings
from coire_node.upgrade_guard import NativeGuardTransport


def scope():
    original = plan()
    value = original.model_copy(
        update={
            "peer_runtime_sha256": "c" * 64,
            "maintenance": original.maintenance.model_copy(
                update={"targets": ("coire-edge-a", "coire-edge-b")}
            ),
        }
    )
    request = NodeUpgradeCollectiveRead(
        generation=value.operation.generation,
        plan_sha256=value.authority_sha256(),
        stage="candidate",
        runtime_sha256=value.target.manifest.environment_sha256,
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
    )
    return NodeUpgradeCollectiveScope(
        plan=value,
        request=request,
        index=0,
        command_id=uuid5(
            NAMESPACE_URL,
            "coire-upgrade-collective/"
            + value.operation.job_id
            + "/candidate/"
            + request.plan_sha256
            + "/0",
        ),
    )


@pytest.mark.parametrize(
    "fault",
    [
        "generation",
        "plan_sha256",
        "runtime_sha256",
        "stage",
        "command_id",
        "index",
        "peer",
        "targets",
    ],
)
def test_collective_scope_refuses_foreign_authority(fault):
    value = scope()
    payload = value.model_dump(mode="json")
    if fault in {"generation", "plan_sha256", "runtime_sha256", "stage"}:
        payload["request"][fault] = {
            "generation": 2,
            "plan_sha256": "f" * 64,
            "runtime_sha256": "f" * 64,
            "stage": "source",
        }[fault]
    elif fault == "command_id":
        payload[fault] = str(uuid4())
    elif fault == "index":
        payload[fault] = 3
    elif fault == "peer":
        payload["plan"]["peer_runtime_sha256"] = None
    else:
        payload["plan"]["maintenance"]["targets"] = ["coire-edge-a"]
    with pytest.raises(ValidationError):
        NodeUpgradeCollectiveScope.model_validate(payload)


def test_collective_roundtrip_retains_exact_pair():
    value = scope()
    assert NodeUpgradeCollectiveScope.model_validate_json(value.model_dump_json()) == value
    assert value.runtime_pair() == (value.request.runtime_sha256, "c" * 64)


def test_cleanup_wire_cannot_request_launch_or_invent_a_measurement():
    from coire_core.models.upgrades import (
        NodeUpgradeCollectiveCleanup,
        NodeUpgradeCollectiveCleanupReceipt,
    )

    original = scope()
    request = NodeUpgradeCollectiveCleanup(scope=original)
    assert NodeUpgradeCollectiveCleanup.model_validate_json(request.model_dump_json()) == request
    assert request.scope.request.deadline_at == original.request.deadline_at
    with pytest.raises(ValidationError):
        NodeUpgradeCollectiveCleanup(scope=original, cleanup_only=False)
    receipt = NodeUpgradeCollectiveCleanupReceipt(
        job_id=original.plan.operation.job_id,
        generation=original.request.generation,
        command_id=original.command_id,
        scope_sha256=original.sha256(),
        namespace_inventory_sha256s={"coire-edge-a": "a" * 64, "coire-edge-b": "b" * 64},
        observed_at=datetime.now(UTC),
    )
    assert receipt.outcome == "interrupted"
    for mutation in ({"outcome": "succeeded"}, {"observations": []}, {"cleanup_verified": False}):
        with pytest.raises(ValidationError):
            NodeUpgradeCollectiveCleanupReceipt.model_validate(receipt.model_dump() | mutation)
    with pytest.raises(ValidationError):
        NodeUpgradeCollectiveCleanupReceipt.model_validate(
            receipt.model_dump() | {"namespace_inventory_sha256s": {"coire-edge-a": "a" * 64}}
        )


def test_guard_collective_missing_peer_has_no_http_effect():
    client = Mock(spec=httpx.Client)
    client.follow_redirects = False
    client._trust_env = False
    owner = NativeGuardTransport(
        Settings(
            node_name="coire-edge-a",
            node_control_host="coire-edge-a.lab",
            node_token=SecretStr("private-test-token"),
        ),
        client,
    )
    value = plan()
    with pytest.raises(UpgradeConflict):
        owner.collective(
            value,
            "a" * 64,
            deadline_at=datetime.now(UTC) + timedelta(seconds=120),
            stage="candidate",
        )
    client.stream.assert_not_called()


def test_unresolved_collective_is_a_persistent_drain_hold(tmp_path):
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node.upgrade_inventory import collective_entries

    value = scope()
    root = PrivateUpgradeStore(tmp_path)
    for part in ("upgrades", "collectives", value.plan.operation.job_id, "candidate", "0"):
        root = PrivateUpgradeStore(root.path(part))
    root.write("scope.json", value)
    first = collective_entries(tmp_path)
    assert len(first) == 1
    assert first[0].state == "cleaning"
    assert first[0].holds == ("unresolved-collective",)
    assert collective_entries(tmp_path) == first


def test_corrupt_collective_owner_is_never_idle(tmp_path):
    from coire_core.errors import UpgradeUnavailable
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node.upgrade_inventory import collective_entries

    PrivateUpgradeStore(tmp_path / "upgrades/collectives" / "private-job/candidate/0")
    with pytest.raises(UpgradeUnavailable):
        collective_entries(tmp_path)


def test_unused_collective_reservation_is_idle_only_under_exclusive_lock(tmp_path):
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node.upgrade_inventory import collective_entries

    value = scope()
    owner = PrivateUpgradeStore(tmp_path)
    for part in ("upgrades", "collectives", value.plan.operation.job_id, "candidate", "0"):
        owner = PrivateUpgradeStore(owner.path(part))
    with owner.ownership("collective.lock"), pytest.raises(UpgradeConflict):
        collective_entries(tmp_path)
    assert collective_entries(tmp_path) == ()
    owner.write("scope.json", value)
    assert collective_entries(tmp_path)[0].holds == ("unresolved-collective",)


def test_unused_collective_reservation_with_foreign_file_is_never_idle(tmp_path):
    from coire_core.errors import UpgradeUnavailable
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node.upgrade_inventory import collective_entries

    value = scope()
    owner = PrivateUpgradeStore(tmp_path)
    for part in ("upgrades", "collectives", value.plan.operation.job_id, "candidate", "0"):
        owner = PrivateUpgradeStore(owner.path(part))
    owner.path("foreign.json").write_text("{}")
    with pytest.raises(UpgradeUnavailable):
        collective_entries(tmp_path)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "foreign_requester",
        "expired",
        "busy_peer",
        "changed_peer",
        "revoked",
        "revoked_during_probe",
        "unavailable_once",
        "failed_collective",
    ],
)
async def test_core_collective_rechecks_exact_accepted_pair(monkeypatch, fault):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_api.upgrades import collective
    from coire_core.errors import UpgradeUnavailable
    from coire_core.models import LinkObservation, ProbeOutcome, ProbeTransport
    from coire_core.models.upgrades import (
        NODE_UPGRADE_OWNERS,
        NodeUpgradeCommand,
        NodeUpgradeInventory,
        NodeUpgradeStatus,
        UpgradeActivationGrant,
        UpgradeOwnerInventory,
        UpgradePhase,
        UpgradeReceipt,
    )

    value = scope()
    plan = value.plan
    now = datetime.now(UTC)
    intent = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=1,
        phase=UpgradePhase.ACTIVATING,
        effect_id="activation",
        outcome="intent",
        evidence="",
        observed_sha256=plan.source.manifest_sha256,
        occurred_at=now - timedelta(seconds=121 if fault == "expired" else 0),
    )
    grant = UpgradeActivationGrant(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        boundary="runtime",
        grant_id=uuid4(),
        expires_at=now + timedelta(seconds=30),
    )
    command = NodeUpgradeCommand.model_validate(
        plan.model_dump(mode="json")
        | {
            "authorization_expires_at": grant.expires_at,
            "activation_grant": grant.model_dump(mode="json"),
            "activation_receipt": intent.model_dump(mode="json"),
            "peer_inventory": NodeUpgradeInventory(
                node="coire-edge-b",
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                runtime_sha256=plan.peer_runtime_sha256,
                observed_at=now,
                owners=(),
            ).model_dump(mode="json"),
        }
    )
    row = SimpleNamespace(
        execution_plan=plan.model_dump(mode="json"),
        payload=plan.operation.model_copy(
            update={"phase": UpgradePhase.ACTIVATING, "receipt_sequence": 1}
        ).model_dump(mode="json"),
        node_command=command.model_dump(mode="json"),
    )
    monkeypatch.setattr(collective, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(collective, "verify_fenced", AsyncMock())
    monkeypatch.setattr(collective, "initiating_actor", lambda _: Mock())
    authorized = AsyncMock(side_effect=UpgradeConflict("revoked") if fault == "revoked" else None)
    monkeypatch.setattr(collective, "authorize", authorized)
    appended = AsyncMock()
    monkeypatch.setattr(collective, "append_observation", appended)
    client = Mock()

    async def status(name):
        digest = value.runtime_pair()[0 if name == "coire-edge-a" else 1]
        return NodeUpgradeStatus(
            node=name,
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            bootstrap_ready=True,
            phase=UpgradePhase.VERIFYING,
            runtime_sha256="d" * 64
            if fault == "changed_peer" and name == "coire-edge-b"
            else digest,
            observed_at=datetime.now(UTC),
        )

    async def inventory(name, job, generation):
        return NodeUpgradeInventory(
            node=name,
            job_id=job,
            generation=generation,
            runtime_sha256=value.runtime_pair()[0 if name == "coire-edge-a" else 1],
            observed_at=datetime.now(UTC),
            owners=tuple(
                UpgradeOwnerInventory(
                    owner=owner,
                    status="unknown"
                    if fault == "busy_peer" and name == "coire-edge-b"
                    else "complete",
                )
                for owner in NODE_UPGRADE_OWNERS
            ),
        )

    async def run(received):
        assert received.plan.authority_sha256() == plan.authority_sha256()
        assert session.commit.await_count >= 2
        if fault == "unavailable_once" and client.run_upgrade_collective.await_count == 1:
            raise NodeError(NodeErrorKind.UNAVAILABLE, "coire-edge-a")
        if fault == "revoked_during_probe":
            authorized.side_effect = UpgradeConflict("revoked during dispatched work")
        return LinkObservation(
            id=uuid4(),
            node_a="coire-edge-a",
            node_b="coire-edge-b",
            transport=ProbeTransport.JACCL,
            outcome=ProbeOutcome.FAILED if fault == "failed_collective" else ProbeOutcome.SUCCEEDED,
            os_version_a="test",
            os_version_b="test",
            engine_version="test",
            bandwidth_bytes_per_second=1000,
            latency_ms=1.0,
            runtime_sha256_a=value.runtime_pair()[0],
            runtime_sha256_b=value.runtime_pair()[1],
            observed_at=datetime.now(UTC),
        )

    client.upgrade_status = AsyncMock(side_effect=status)
    client.upgrade_inventory = AsyncMock(side_effect=inventory)
    client.run_upgrade_collective = AsyncMock(side_effect=run)
    session = Mock()
    session.get = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    body = value.request.model_copy(
        update={
            "deadline_at": intent.occurred_at + timedelta(seconds=plan.operation.timing.readiness_s)
        }
    )
    if fault not in {None, "failed_collective"}:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            await collective.qualify_collective(
                session,
                "coire-edge-b" if fault == "foreign_requester" else "coire-edge-a",
                plan.operation.job_id,
                body,
                client,
            )
        if fault in {"revoked_during_probe", "unavailable_once"}:
            assert client.run_upgrade_collective.await_count == 1
        else:
            client.run_upgrade_collective.assert_not_awaited()
        appended.assert_not_awaited()
        if fault == "unavailable_once":
            first_scope = client.run_upgrade_collective.call_args.args[0]
            result = await collective.qualify_collective(
                session, "coire-edge-a", plan.operation.job_id, body, client
            )
            assert client.run_upgrade_collective.call_args_list[1].args[0] == first_scope
            assert len(result.observations) == 3
            assert appended.await_count == 3
            assert result.deadline_at == body.deadline_at
    else:
        result = await collective.qualify_collective(
            session, "coire-edge-a", plan.operation.job_id, body, client
        )
        count = 1 if fault == "failed_collective" else 3
        assert len(result.observations) == count
        assert client.run_upgrade_collective.await_count == count
        assert appended.await_count == count
        assert result.deadline_at == body.deadline_at
