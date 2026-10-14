"""Accepted native upgrade/rollback through a provisioned private API and real nodes.

The fixture must be provisioned by the isolated native acceptance setup. This
suite never discovers production credentials or endpoints and never substitutes
fake engines, serving output, collective evidence or SQL authority.
"""

import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import yaml

from coire_core.models.auth import ApiKeyCreate, ApiKeyIssued, AuthScope
from coire_core.models.training import (
    TrainingJobDetail,
    TrainingJobReceipt,
    TrainingJobState,
    TrainingReplayPage,
    TrainingSubmission,
    TrainingValidation,
)
from coire_core.models.upgrades import (
    NodeUpgradeInventory,
    UpgradeCancelRequest,
    UpgradeEventPage,
    UpgradeEvidence,
    UpgradeOperation,
    UpgradePhase,
    UpgradeRelease,
    UpgradeRollbackRequest,
    UpgradeStartRequest,
    UpgradeTargetList,
)

pytestmark = [pytest.mark.integration, pytest.mark.native_upgrade]


def _private_fixture() -> tuple[Path, dict[str, object]]:
    configured = os.environ.get("COIRE_UPGRADE_NATIVE_FIXTURE")
    if not configured:
        raise RuntimeError(
            "COIRE_UPGRADE_NATIVE_FIXTURE requires the provisioned private native fixture"
        )
    path = Path(configured)
    details = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
        or details.st_nlink != 1
        or path.parent.is_symlink()
        or path.parent.stat().st_mode & 0o077
    ):
        raise RuntimeError("Native fixture configuration is not private and owned")
    body = json.loads(path.read_bytes())
    endpoint = urlsplit(str(body.get("core_url", "")))
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.port is None
        or not 1024 <= endpoint.port <= 65535
        or endpoint.username
        or endpoint.password
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
    ):
        raise RuntimeError("Native acceptance permits only its owned loopback Core API")
    return path.parent, body


def _operation(client: httpx.Client, route: str, body: dict[str, object]) -> UpgradeOperation:
    response = client.post(route, json=body)
    retry_until = time.monotonic() + 30
    while (
        route == "/api/v1/admin/upgrades/nodes/coire-edge-a"
        and response.status_code == 409
        and response.json().get("detail")
        == "Studio Source or independent recovery readiness differs"
        and time.monotonic() < retry_until
    ):
        # No operation was accepted. Keep the exact request/idempotency key and
        # require a fresh authoritative admission check on every retry.
        time.sleep(0.5)
        response = client.post(route, json=body)
    if response.status_code not in {200, 202}:
        reason = response.json().get("detail", "unavailable")
        raise AssertionError(f"Native operation refused with HTTP {response.status_code}: {reason}")
    return UpgradeOperation.model_validate(response.json())


def _finish(
    client: httpx.Client,
    accepted: UpgradeOperation,
    expected: UpgradePhase = UpgradePhase.SUCCEEDED,
) -> UpgradeOperation:
    # The timeout bounds the test; it grants no renewable runtime epoch.
    limit = time.monotonic() + (
        accepted.timing.drain_s + accepted.timing.readiness_s + accepted.timing.recovery_s + 30
    )
    previous = None
    while time.monotonic() < limit:
        response = client.get(f"/api/v1/admin/upgrades/{accepted.job_id}")
        response.raise_for_status()
        operation = UpgradeOperation.model_validate(response.json())
        if operation.phase != previous:
            print(
                json.dumps({"job_id": operation.job_id, "phase": operation.phase.value}), flush=True
            )
            previous = operation.phase
        if operation.phase is UpgradePhase.RECOVERY_REQUIRED:
            raise AssertionError("Native operation retained recovery-required ownership")
        if operation.finished_at is not None:
            if operation.phase is not expected:
                raise AssertionError(f"Native transition ended {operation.phase.value}")
            return operation
        time.sleep(0.5)
    raise AssertionError("Native operation exceeded its original acceptance bounds")


def _ready(client: httpx.Client, manifest_sha256: str) -> None:
    limit = time.monotonic() + 30
    while time.monotonic() < limit:
        response = client.get("/api/v1/admin/upgrades/targets")
        response.raise_for_status()
        targets = UpgradeTargetList.model_validate(response.json())
        selected = next(item for item in targets.items if item.node == "coire-edge-a")
        peers_ready = all(
            item.readiness == "ready" for item in targets.items if item.kind.value == "studio"
        )
        if (
            selected.readiness == "ready"
            and selected.observed_sha256 == manifest_sha256
            and peers_ready
        ):
            return
        time.sleep(0.5)
    raise AssertionError(
        "Native terminal transition did not release and refresh its recovery owner"
    )


def test_accepted_tiny_model_upgrade_and_manual_rollback() -> None:
    root, fixture = _private_fixture()
    source = UpgradeRelease.model_validate_json((root / "source-coire-edge-a.json").read_bytes())
    target = UpgradeRelease.model_validate_json((root / "target.json").read_bytes())
    credential = fixture.get("admin_key")
    if not isinstance(credential, str) or not credential.startswith("coire_"):
        raise RuntimeError("Native acceptance requires its user-bound issued API key")
    with httpx.Client(
        base_url=str(fixture["core_url"]),
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + credential},
        timeout=15,
    ) as client:
        _ready(client, source.manifest_sha256)
        request = UpgradeStartRequest(
            release_id=target.release_id,
            expected_source_sha256=source.manifest_sha256,
            idempotency_key=uuid4(),
            acknowledge_maintenance=True,
        )
        accepted = _operation(
            client,
            "/api/v1/admin/upgrades/nodes/coire-edge-a",
            request.model_dump(mode="json"),
        )
        replay = _operation(
            client,
            "/api/v1/admin/upgrades/nodes/coire-edge-a",
            request.model_dump(mode="json"),
        )
        assert replay.job_id == accepted.job_id and replay.generation == accepted.generation
        completed = _finish(client, accepted)
        _ready(client, target.manifest_sha256)
        reversed_operation = _operation(
            client,
            f"/api/v1/admin/upgrades/{completed.job_id}/rollback",
            UpgradeRollbackRequest(
                expected_source_sha256=target.manifest_sha256,
                idempotency_key=uuid4(),
                acknowledge_maintenance=True,
            ).model_dump(mode="json"),
        )
        restored = _finish(client, reversed_operation)
        assert restored.parent_job_id == completed.job_id
        assert restored.target_sha256 == source.manifest_sha256
        _ready(client, source.manifest_sha256)
        measured = {
            "forward": completed.model_dump(mode="json"),
            "rollback": restored.model_dump(mode="json"),
        }
        evidence = root / "native-upgrade-rollback-result.json"
        evidence.write_text(json.dumps(measured, sort_keys=True))
        evidence.chmod(0o600)


@pytest.mark.parametrize("scenario", ["post_switch_timeout", "helper_restart", "broken_candidate"])
def test_missing_candidate_listener_restores_exact_source(scenario: str) -> None:
    from upgrade_native_faults import native_fault

    root, fixture = _private_fixture()
    source = UpgradeRelease.model_validate_json((root / "source-coire-edge-a.json").read_bytes())
    target_file = "broken-target.json" if scenario == "broken_candidate" else "target.json"
    target = UpgradeRelease.model_validate_json((root / target_file).read_bytes())
    credential = fixture.get("admin_key")
    assert isinstance(credential, str)
    with httpx.Client(
        base_url=str(fixture["core_url"]),
        trust_env=False,
        headers={"Authorization": "Bearer " + credential},
        timeout=15,
    ) as client:
        _ready(client, source.manifest_sha256)
        blocked = scenario != "broken_candidate"
        if blocked:
            assert native_fault(root, fixture, "block")["applied"] is True
        try:
            accepted = _operation(
                client,
                "/api/v1/admin/upgrades/nodes/coire-edge-a",
                UpgradeStartRequest(
                    release_id=target.release_id,
                    expected_source_sha256=source.manifest_sha256,
                    idempotency_key=uuid4(),
                    acknowledge_maintenance=True,
                ).model_dump(mode="json"),
            )
            # A refusal before selection does not exercise a broken candidate
            # or recovery from a missing listener. Require the durable boundary
            # for every fault case, rather than accepting a vacuous restoration.
            deadline = time.monotonic() + accepted.timing.drain_s + accepted.timing.readiness_s
            while time.monotonic() < deadline:
                selected = native_fault(
                    root,
                    fixture,
                    "restart_selected_guard"
                    if scenario == "helper_restart"
                    else "observe_selected_candidate",
                    job_id=accepted.job_id,
                    target_runtime_sha256=target.manifest.environment_sha256,
                )
                if selected["applied"] is True:
                    assert selected["generation"] == accepted.generation
                    break
                response = client.get(f"/api/v1/admin/upgrades/{accepted.job_id}")
                response.raise_for_status()
                current = UpgradeOperation.model_validate(response.json())
                assert (
                    current.finished_at is None
                    and current.phase is not UpgradePhase.RECOVERY_REQUIRED
                ), "Operation ended before the required fault boundary"
                time.sleep(0.5)
            else:
                raise AssertionError("Candidate never reached its durable selected boundary")
            restored = _finish(client, accepted, UpgradePhase.ROLLED_BACK)
            _ready(client, source.manifest_sha256)
            response = client.get(f"/api/v1/admin/upgrades/{restored.job_id}/events")
            response.raise_for_status()
            page = UpgradeEventPage.model_validate(response.json())
            activation = next(
                event for event in page.events if event.phase is UpgradePhase.ACTIVATING
            )
            completed = next(
                event
                for event in page.events
                if event.phase is UpgradePhase.VERIFYING_SOURCE and event.outcome == "complete"
            )
            proof = UpgradeEvidence.model_validate_json(completed.evidence)
            assert proof.source_verified is True and proof.cleanup_verified is True
            assert proof.generated_tokens is not None and proof.generated_tokens >= 10
            assert proof.runtime_sha256 == source.manifest.environment_sha256
            if proof.collective_verified is not True:
                # FR-012/T032 explicitly permits restored serving with failed
                # Source-pair qualification, but older successful evidence must
                # no longer authorize paired placement.
                from coire_core.models import ProbeOutcome, ProbeTransport, StudioLinkProjection

                link_response = client.get("/api/v1/admin/links/studios")
                link_response.raise_for_status()
                link = StudioLinkProjection.model_validate(link_response.json())
                assert not link.tp_eligible
                latest = max(
                    (item for item in link.latest if item.transport is ProbeTransport.JACCL),
                    key=lambda item: item.observed_at,
                )
                assert latest.outcome is ProbeOutcome.FAILED
                assert latest.reason == "restored source collective qualification unavailable"
                assert latest.runtime_sha256_a == source.manifest.environment_sha256
                assert latest.runtime_sha256_b == proof.peer_runtime_sha256
                assert latest.observed_at == completed.occurred_at
            assert completed.observed_sha256 == source.manifest_sha256
            elapsed = (completed.occurred_at - activation.occurred_at).total_seconds()
            assert elapsed <= accepted.timing.readiness_s + accepted.timing.recovery_s
            result = root / (scenario.replace("_", "-") + "-result.json")
            result.write_text(
                json.dumps(
                    {
                        "operation": restored.model_dump(mode="json"),
                        "restoration_elapsed_s": elapsed,
                        "events": page.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
            )
            result.chmod(0o600)
        finally:
            if blocked:
                native_fault(root, fixture, "unblock")


def test_native_terminal_replay_and_cli_watch_within_ten_seconds() -> None:
    import subprocess

    root, fixture = _private_fixture()
    recorded = json.loads((root / "native-upgrade-rollback-result.json").read_bytes())
    operation = UpgradeOperation.model_validate(recorded["rollback"])
    cursor = max(0, operation.receipt_sequence - 3)
    credential = fixture.get("admin_key")
    assert isinstance(credential, str)
    measured = {}
    with httpx.Client(
        base_url=str(fixture["core_url"]),
        trust_env=False,
        headers={"Authorization": "Bearer " + credential},
        timeout=10,
    ) as client:
        start = time.monotonic()
        response = client.get(
            f"/api/v1/admin/upgrades/{operation.job_id}/events", params={"after": cursor}
        )
        response.raise_for_status()
        page = UpgradeEventPage.model_validate(response.json())
        measured["events_s"] = time.monotonic() - start
        assert page.cursor == operation.receipt_sequence
        assert [event.sequence for event in page.events] == list(range(cursor + 1, page.cursor + 1))
        assert page.operation.phase is UpgradePhase.SUCCEEDED
        start = time.monotonic()
        response = client.get(
            f"/api/v1/admin/upgrades/{operation.job_id}/events",
            headers={"Accept": "text/event-stream", "Last-Event-ID": str(cursor)},
        )
        response.raise_for_status()
        measured["sse_s"] = time.monotonic() - start
        assert response.text.count("event: progress") == len(page.events)
        assert "event: snapshot" in response.text and f"id: {page.cursor}" in response.text
        start = time.monotonic()
        response = client.get(
            f"/api/v1/admin/upgrades/{operation.job_id}/events", params={"after": page.cursor + 1}
        )
        response.raise_for_status()
        reset = UpgradeEventPage.model_validate(response.json())
        measured["reset_s"] = time.monotonic() - start
        assert reset.reset and reset.cursor == page.cursor
    env = {**os.environ, "COIRE_API_URL": str(fixture["core_url"]), "COIRE_API_TOKEN": credential}
    start = time.monotonic()
    result = subprocess.run(
        ["uv", "run", "coire", "upgrades", "status", operation.job_id, "--watch", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    measured["cli_s"] = time.monotonic() - start
    assert (
        result.returncode == 0
        and operation.job_id in result.stdout
        and "succeeded" in result.stdout
    )
    assert all(elapsed <= 10 for elapsed in measured.values())
    evidence = root / "native-replay-result.json"
    evidence.write_text(json.dumps(measured))
    evidence.chmod(0o600)


@pytest.mark.parametrize("action", ["cancel", "revoke"])
def test_selected_candidate_cancellation_or_revocation_restores_source(action: str) -> None:
    from upgrade_native_faults import native_fault

    root, fixture = _private_fixture()
    source = UpgradeRelease.model_validate_json((root / "source-coire-edge-a.json").read_bytes())
    target = UpgradeRelease.model_validate_json((root / "target.json").read_bytes())
    credential = fixture.get("admin_key")
    assert isinstance(credential, str)
    with httpx.Client(
        base_url=str(fixture["core_url"]),
        trust_env=False,
        headers={"Authorization": "Bearer " + credential},
        timeout=15,
    ) as client:
        _ready(client, source.manifest_sha256)
        issued = None
        if action == "revoke":
            response = client.post(
                f"/api/v1/admin/users/{fixture['owner']}/keys",
                json=ApiKeyCreate(
                    name="isolated-upgrade-revocation",
                    scopes=frozenset({AuthScope.ADMIN}),
                    requests_per_minute=10000,
                    monthly_budget_tokens=1000000,
                ).model_dump(mode="json"),
            )
            assert response.status_code == 201
            issued = ApiKeyIssued.model_validate(response.json())
        blocked = action == "revoke"
        if blocked:
            assert native_fault(root, fixture, "block")["applied"] is True
        try:
            headers = {"Authorization": "Bearer " + issued.secret} if issued else {}
            response = client.post(
                "/api/v1/admin/upgrades/nodes/coire-edge-a",
                headers=headers,
                json=UpgradeStartRequest(
                    release_id=target.release_id,
                    expected_source_sha256=source.manifest_sha256,
                    idempotency_key=uuid4(),
                    acknowledge_maintenance=True,
                ).model_dump(mode="json"),
            )
            assert response.status_code == 202
            accepted = UpgradeOperation.model_validate(response.json())
            deadline = time.monotonic() + accepted.timing.drain_s + accepted.timing.readiness_s
            while time.monotonic() < deadline:
                selected = native_fault(
                    root,
                    fixture,
                    "observe_selected_candidate",
                    job_id=accepted.job_id,
                    target_runtime_sha256=target.manifest.environment_sha256,
                )
                if selected["applied"] is True:
                    assert selected["generation"] == accepted.generation
                    break
                response = client.get(f"/api/v1/admin/upgrades/{accepted.job_id}")
                response.raise_for_status()
                current = UpgradeOperation.model_validate(response.json())
                assert (
                    current.finished_at is None
                    and current.phase is not UpgradePhase.RECOVERY_REQUIRED
                ), "Operation ended before the required fault boundary"
                time.sleep(0.5)
            else:
                raise AssertionError("Candidate never durably selected within its original window")
            if issued:
                response = client.delete(f"/api/v1/admin/keys/{issued.id}")
                assert response.status_code == 204
                denied = client.get(
                    f"/api/v1/admin/upgrades/{accepted.job_id}",
                    headers={"Authorization": "Bearer " + issued.secret},
                )
                assert denied.status_code in {401, 403}
            else:
                # A lost listener acknowledgement is a retry response, not an
                # accepted cancellation. Retry only inside the captured epoch.
                captured = selected.get("readiness_deadline_at")
                assert isinstance(captured, str)
                original_deadline = datetime.fromisoformat(captured)
                while datetime.now(UTC) < original_deadline:
                    response = client.post(
                        f"/api/v1/admin/upgrades/{accepted.job_id}/cancel",
                        json=UpgradeCancelRequest().model_dump(mode="json"),
                    )
                    if response.status_code == 202:
                        acknowledged = UpgradeOperation.model_validate(response.json())
                        assert (
                            acknowledged.cancel_requested
                            or acknowledged.phase is UpgradePhase.ROLLED_BACK
                        )
                        break
                    assert response.status_code == 503
                    time.sleep(0.5)
                else:
                    raise AssertionError("Cancellation never acknowledged within original epoch")
            restored = _finish(client, accepted, UpgradePhase.ROLLED_BACK)
            _ready(client, source.manifest_sha256)
            response = client.get(f"/api/v1/admin/upgrades/{restored.job_id}/events")
            response.raise_for_status()
            page = UpgradeEventPage.model_validate(response.json())
            receipt = next(
                event
                for event in page.events
                if event.phase is UpgradePhase.VERIFYING_SOURCE and event.outcome == "complete"
            )
            proof = UpgradeEvidence.model_validate_json(receipt.evidence)
            assert proof.source_verified and proof.cleanup_verified
            assert proof.runtime_sha256 == source.manifest.environment_sha256
            assert proof.generated_tokens is not None and proof.generated_tokens >= 10
            result = root / ("selected-" + action + "-result.json")
            result.write_text(restored.model_dump_json())
            result.chmod(0o600)
        finally:
            if blocked:
                native_fault(root, fixture, "unblock")
            if issued:
                # Idempotent revocation also covers failures before the fault boundary.
                response = client.delete(f"/api/v1/admin/keys/{issued.id}")
                assert response.status_code in {204, 404}


def test_cancel_before_activation_keeps_exact_source() -> None:
    root, fixture = _private_fixture()
    source = UpgradeRelease.model_validate_json((root / "source-coire-edge-a.json").read_bytes())
    target = UpgradeRelease.model_validate_json((root / "target.json").read_bytes())
    credential = fixture.get("admin_key")
    assert isinstance(credential, str)
    with httpx.Client(
        base_url=str(fixture["core_url"]),
        trust_env=False,
        headers={"Authorization": "Bearer " + credential},
        timeout=15,
    ) as client:
        _ready(client, source.manifest_sha256)
        accepted = _operation(
            client,
            "/api/v1/admin/upgrades/nodes/coire-edge-a",
            UpgradeStartRequest(
                release_id=target.release_id,
                expected_source_sha256=source.manifest_sha256,
                idempotency_key=uuid4(),
                acknowledge_maintenance=True,
            ).model_dump(mode="json"),
        )
        cancel_deadline = time.monotonic() + 30
        while time.monotonic() < cancel_deadline:
            response = client.post(
                f"/api/v1/admin/upgrades/{accepted.job_id}/cancel",
                json=UpgradeCancelRequest().model_dump(mode="json"),
            )
            if response.status_code == 202:
                requested = UpgradeOperation.model_validate(response.json())
                assert (
                    requested.cancel_requested
                    or requested.phase is UpgradePhase.CANCELLED_UNCHANGED
                )
                break
            assert response.status_code == 503
            observed = client.get(f"/api/v1/admin/upgrades/{accepted.job_id}")
            observed.raise_for_status()
            current = UpgradeOperation.model_validate(observed.json())
            assert current.phase not in {
                UpgradePhase.ACTIVATING,
                UpgradePhase.VERIFYING,
                UpgradePhase.COMMITTING,
                UpgradePhase.SUCCEEDED,
            }
            time.sleep(0.5)
        else:
            raise AssertionError("Pre-activation cancellation never acknowledged")
        unchanged = _finish(client, accepted, UpgradePhase.CANCELLED_UNCHANGED)
        _ready(client, source.manifest_sha256)
        response = client.get(f"/api/v1/admin/upgrades/{unchanged.job_id}/events")
        response.raise_for_status()
        page = UpgradeEventPage.model_validate(response.json())
        assert not any(event.phase is UpgradePhase.ACTIVATING for event in page.events)
        path = root / "pre-activation-cancel-result.json"
        path.write_text(unchanged.model_dump_json())
        path.chmod(0o600)


def test_busy_training_aborts_unchanged_and_commits_checkpoint_normally() -> None:
    root, fixture = _private_fixture()
    recipe = root / "training-source.yaml"
    details = recipe.lstat()
    assert stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
    assert not details.st_mode & 0o077 and details.st_nlink == 1
    document = yaml.safe_load(recipe.read_bytes())
    document["output"]["adapter_slug"] = "upgrade-drain-" + uuid4().hex[:12]
    submission = TrainingSubmission(source_yaml=yaml.safe_dump(document))
    source = UpgradeRelease.model_validate_json((root / "source-coire-edge-a.json").read_bytes())
    target = UpgradeRelease.model_validate_json((root / "target.json").read_bytes())
    endpoint = urlsplit(str(fixture.get("node_control_url", "")))
    assert endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1"
    assert endpoint.port is not None and 1024 <= endpoint.port <= 65535
    assert not any(
        (endpoint.username, endpoint.password, endpoint.path, endpoint.query, endpoint.fragment)
    )
    tokens = fixture["node_tokens"]
    assert isinstance(tokens, dict) and isinstance(tokens["coire-edge-a"], str)
    with (
        httpx.Client(
            base_url=str(fixture["core_url"]),
            trust_env=False,
            headers={"Authorization": "Bearer " + str(fixture["admin_key"])},
            timeout=30,
        ) as client,
        httpx.Client(
            base_url=endpoint.geturl(),
            trust_env=False,
            headers={"Authorization": "Bearer " + tokens["coire-edge-a"]},
            timeout=10,
        ) as node,
    ):
        _ready(client, source.manifest_sha256)
        response = client.post(
            "/api/v1/admin/training/validate", json=submission.model_dump(mode="json")
        )
        response.raise_for_status()
        validation = TrainingValidation.model_validate(response.json())
        assert validation.ready_to_run and validation.resolved is not None
        response = client.post(
            "/api/v1/admin/training/jobs",
            headers={"Idempotency-Key": str(uuid4())},
            json=submission.model_dump(mode="json"),
        )
        assert response.status_code == 202
        training = TrainingJobReceipt.model_validate(response.json())
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/admin/training/jobs/{training.job_id}")
            response.raise_for_status()
            detail = TrainingJobDetail.model_validate(response.json())
            if detail.state is TrainingJobState.RUNNING:
                break
            assert detail.state not in {
                TrainingJobState.SUCCEEDED,
                TrainingJobState.FAILED,
                TrainingJobState.CANCELLED,
            }, "Training ended before maintenance could exercise busy ownership"
            time.sleep(0.25)
        else:
            raise AssertionError("Measured training did not become active")
        accepted = _operation(
            client,
            "/api/v1/admin/upgrades/nodes/coire-edge-a",
            UpgradeStartRequest(
                release_id=target.release_id,
                expected_source_sha256=source.manifest_sha256,
                idempotency_key=uuid4(),
                acknowledge_maintenance=True,
            ).model_dump(mode="json"),
        )
        assert accepted.timing.drain_s <= 15
        positive: list[NodeUpgradeInventory] = []
        deadline = time.monotonic() + accepted.timing.drain_s + 30
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/admin/upgrades/{accepted.job_id}")
            response.raise_for_status()
            operation = UpgradeOperation.model_validate(response.json())
            if operation.finished_at is not None:
                break
            assert operation.phase in {UpgradePhase.ACCEPTED, UpgradePhase.DRAINING}
            response = node.get(
                f"/node/maintenance/{accepted.job_id}/inventory",
                params={"generation": accepted.generation},
            )
            assert response.status_code in {200, 409}
            if response.status_code == 200:
                census = NodeUpgradeInventory.model_validate(response.json())
                if any(
                    owner.owner == "training-lifecycle"
                    and any(
                        entry.work_id == training.job_id and entry.state == "active"
                        for entry in owner.entries
                    )
                    for owner in census.owners
                ):
                    positive.append(census)
            time.sleep(0.5)
        else:
            raise AssertionError("Busy upgrade did not close within its captured drain budget")
        assert positive and operation.phase is UpgradePhase.FAILED_UNCHANGED
        assert operation.timing == accepted.timing
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/admin/training/jobs/{training.job_id}")
            response.raise_for_status()
            detail = TrainingJobDetail.model_validate(response.json())
            if detail.state is TrainingJobState.SUCCEEDED:
                break
            assert detail.state not in {TrainingJobState.FAILED, TrainingJobState.CANCELLED}
            time.sleep(1)
        else:
            raise AssertionError(
                "Existing training did not complete after unchanged admission reopened"
            )
        assert detail.completed_update == validation.spec.optim.updates
        assert detail.latest_checkpoint_id is not None and detail.adapter_id is not None
        cursor = 0
        attempts: set[str] = set()
        for _ in range(20):
            response = client.get(training.events_path, params={"after": cursor})
            response.raise_for_status()
            page = TrainingReplayPage.model_validate(response.json())
            assert page.reset is None, "Training evidence expired before acceptance verification"
            assert not any(event.kind == "recovery" for event in page.events)
            attempts.update(
                event.attempt_id for event in page.events if event.attempt_id is not None
            )
            if any(event.kind == "terminal" for event in page.events):
                break
            assert page.cursor > cursor
            cursor = page.cursor
        else:
            raise AssertionError("Training history exceeded the bounded acceptance replay")
        assert len(attempts) == 1
        _ready(client, source.manifest_sha256)
        evidence = root / "busy-training-drain-result.json"
        evidence.write_text(
            json.dumps(
                {
                    "upgrade": operation.model_dump(mode="json"),
                    "training_id": detail.id,
                    "completed_updates": detail.completed_update,
                    "attempt_count": len(attempts),
                    "checkpoint_id": str(detail.latest_checkpoint_id),
                    "adapter_id": str(detail.adapter_id),
                    "positive_inventory_count": len(positive),
                },
                sort_keys=True,
            )
        )
        evidence.chmod(0o600)
