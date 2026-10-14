"""Real image/agent maintenance drain on a provisioned private native fixture.

Requests are prepared separately through ordinary acquisition/workspace paths.
No engines, owner inventories, qualifications or completion results are mocked.
"""

import json
import os
import stat
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
import pytest
from integration.test_upgrade_node import _finish, _operation, _private_fixture, _ready

from coire_core.models.auth import ApiKeyCreate, ApiKeyIssued, AuthScope
from coire_core.models.images import ImageSubmitRequest
from coire_core.models.runs import AgentRunCreate
from coire_core.models.upgrades import (
    NodeUpgradeInventory,
    UpgradeOperation,
    UpgradePhase,
    UpgradeRelease,
    UpgradeRollbackRequest,
    UpgradeStartRequest,
)

pytestmark = [pytest.mark.integration, pytest.mark.native_upgrade]


def _request_bytes(path: Path) -> bytes:
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
        or details.st_nlink != 1
        or details.st_size > 64 * 1024
    ):
        raise RuntimeError("Native workload request must be bounded, private and owned")
    return path.read_bytes()


def _save(root: Path, name: str, body: object) -> None:
    path = root / name
    # Evidence is always a new file; never follow or overwrite existing authority.
    with path.open("x") as output:
        path.chmod(0o600)
        json.dump(body, output)


def _work_settled(client: httpx.Client, route: str, work_id: str, initiating_key: str) -> bool:
    """Unknown transport state never authorizes revocation of the initiating key."""
    try:
        response = client.get(
            route + "/" + work_id,
            headers={"Authorization": "Bearer " + initiating_key},
        )
    except httpx.TransportError:
        return False
    return response.status_code == 200 and response.json()["state"] in {
        "succeeded",
        "failed",
        "cancelled",
        "killed",
        "timed_out",
    }


@pytest.mark.parametrize("workload", ["image", "agent"])
def test_active_work_completes_before_native_activation(
    workload: Literal["image", "agent"],
) -> None:
    root, fixture = _private_fixture()
    source = UpgradeRelease.model_validate_json((root / "source-coire-edge-a.json").read_bytes())
    target = UpgradeRelease.model_validate_json((root / "target.json").read_bytes())
    request_bytes = _request_bytes(root / (workload + "-drain-request.json"))
    request = (
        ImageSubmitRequest.model_validate_json(request_bytes)
        if workload == "image"
        else AgentRunCreate.model_validate_json(request_bytes)
    )
    if isinstance(request, AgentRunCreate):
        assert request.task_class == "read" and not request.permitted_tools
    prefix = workload + "-native-drain-" + uuid4().hex
    with httpx.Client(
        base_url=str(fixture["core_url"]),
        headers={"Authorization": "Bearer " + str(fixture["admin_key"])},
        trust_env=False,
        timeout=30,
    ) as admin:
        _ready(admin, source.manifest_sha256)
        actor_id = str(fixture["owner"])
        if workload == "agent":
            # Assets and their validation/publication are prepared independently
            # through normal admin paths. Read-only operator acceptance uses
            # the real administrator; this test changes no asset entitlements.
            actor_response = admin.get("/api/v1/me")
            actor_response.raise_for_status()
            assert actor_response.json()["role"] == "admin"
            actor_id = str(actor_response.json()["id"])
        issued = admin.post(
            f"/api/v1/admin/users/{actor_id}/keys",
            json=ApiKeyCreate(
                name="isolated-native-" + workload + "-drain",
                scopes=frozenset({AuthScope.IMAGES if workload == "image" else AuthScope.ADMIN}),
                requests_per_minute=10000,
                monthly_budget_tokens=1000000,
            ).model_dump(mode="json"),
        )
        issued.raise_for_status()
        key = ApiKeyIssued.model_validate(issued.json())
        tokens = fixture["node_tokens"]
        assert isinstance(tokens, dict)
        _save(root, prefix + "-observer-key.json", {"key_id": str(key.id)})
        work_id: str | None = None
        submission_attempted = False
        route = "/api/v1/images" if workload == "image" else "/api/v1/runs"
        try:
            with (
                httpx.Client(
                    base_url=str(fixture["core_url"]),
                    headers={"Authorization": "Bearer " + key.secret},
                    trust_env=False,
                    timeout=30,
                ) as user,
                httpx.Client(
                    base_url=str(fixture["node_control_url"]),
                    headers={"Authorization": "Bearer " + str(tokens["coire-edge-a"])},
                    trust_env=False,
                    timeout=10,
                ) as node,
            ):
                submission_attempted = True
                request_key = str(uuid4())
                _save(root, prefix + "-submission-key.json", {"idempotency_key": request_key})
                submitted = user.post(
                    route,
                    json=request.model_dump(mode="json"),
                    headers={"Idempotency-Key": request_key},
                )
                if submitted.status_code != 202:
                    _save(
                        root,
                        prefix + "-submission-refused.json",
                        {"status": submitted.status_code, "problem": submitted.json()},
                    )
                    if 400 <= submitted.status_code < 500:
                        submission_attempted = False  # Explicit admission refusal: no new work.
                assert submitted.status_code == 202
                work_id = str(submitted.json()["job_id" if workload == "image" else "id"])
                _save(root, prefix + "-accepted.json", submitted.json())
                observation_deadline = time.monotonic() + 600
                while time.monotonic() < observation_deadline:
                    response = user.get(route + "/" + work_id)
                    response.raise_for_status()
                    detail = response.json()
                    assert detail["state"] not in {
                        "failed",
                        "cancelled",
                        "killed",
                        "timed_out",
                        "succeeded",
                    }, "Work ended before maintenance could observe active ownership"
                    if workload == "image":
                        try:
                            live = node.get(
                                "/node/images/jobs/" + work_id, params={"attempt": 1, "fence": 1}
                            )
                        except httpx.TransportError:
                            # Cold native startup may delay an observation. Retry
                            # reads only inside the original observation budget.
                            continue
                        assert live.status_code in {200, 404, 409, 503}
                        active = live.status_code == 200 and live.json()["state"] == "running"
                    else:
                        # A started container may still be waiting for placement.
                        # Require actual gateway inference before fencing its owner.
                        instances = admin.get("/api/v1/instances")
                        instances.raise_for_status()
                        assert isinstance(request, AgentRunCreate)
                        active = detail["state"] == "running" and any(
                            instance["state"] == "ready"
                            and instance["in_flight"] > 0
                            and instance["model_id"] == str(request.primary_model_id)
                            for instance in instances.json()
                        )
                    if active:
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("No actual active workload within observation bound")
                accepted = _operation(
                    admin,
                    "/api/v1/admin/upgrades/nodes/coire-edge-a",
                    UpgradeStartRequest(
                        release_id=target.release_id,
                        expected_source_sha256=source.manifest_sha256,
                        idempotency_key=uuid4(),
                        acknowledge_maintenance=True,
                    ).model_dump(mode="json"),
                )
                _save(root, prefix + "-upgrade.json", accepted.model_dump(mode="json"))
                deadline = time.monotonic() + (
                    accepted.timing.drain_s
                    + accepted.timing.readiness_s
                    + accepted.timing.recovery_s
                    + 30
                )
                positive: list[dict[str, object]] = []
                completed = False
                while time.monotonic() < deadline:
                    response = user.get(route + "/" + work_id)
                    response.raise_for_status()
                    detail = response.json()
                    assert detail["state"] not in {"failed", "cancelled", "killed", "timed_out"}
                    completed = completed or detail["state"] == "succeeded"
                    observed = admin.get("/api/v1/admin/upgrades/" + accepted.job_id)
                    observed.raise_for_status()
                    operation = UpgradeOperation.model_validate(observed.json())
                    if not completed:
                        assert operation.phase not in {
                            UpgradePhase.ACTIVATING,
                            UpgradePhase.VERIFYING,
                            UpgradePhase.COMMITTING,
                            UpgradePhase.SUCCEEDED,
                        }, "Activation preceded normal work completion"
                    try:
                        census_response = node.get(
                            "/node/maintenance/" + accepted.job_id + "/inventory",
                            params={"generation": accepted.generation},
                        )
                    except httpx.TransportError:
                        assert completed, "Node lost before workload completed"
                    else:
                        if census_response.status_code == 200:
                            census = NodeUpgradeInventory.model_validate(census_response.json())
                            identity = (
                                work_id if workload == "image" else detail.get("container_id")
                            )
                            if any(
                                entry.work_id == identity and entry.state == "active"
                                for owner in census.owners
                                for entry in owner.entries
                            ):
                                positive.append(census.model_dump(mode="json"))
                                _save(
                                    root,
                                    prefix + f"-busy-{len(positive):06d}.json",
                                    positive[-1],
                                )
                        elif census_response.status_code != 409:
                            assert completed and census_response.status_code in {500, 503}
                    if operation.finished_at is not None:
                        # Preserve actual terminal failure as well as success;
                        # an acceptance assertion must not discard drain proof.
                        _save(
                            root,
                            prefix + "-result.json",
                            {
                                "operation": operation.model_dump(mode="json"),
                                "work_id": work_id,
                                "positive_censuses": positive,
                                "normal_completion": completed,
                            },
                        )
                        assert operation.phase is UpgradePhase.SUCCEEDED
                        assert completed and positive, "No positive authenticated busy owner census"
                        break
                    assert operation.phase is not UpgradePhase.RECOVERY_REQUIRED
                    time.sleep(0.1)
                else:
                    raise AssertionError("Native workload drain exceeded original operation bounds")
                _ready(admin, target.manifest_sha256)
                rollback = _operation(
                    admin,
                    "/api/v1/admin/upgrades/" + accepted.job_id + "/rollback",
                    UpgradeRollbackRequest(
                        expected_source_sha256=target.manifest_sha256,
                        idempotency_key=uuid4(),
                        acknowledge_maintenance=True,
                    ).model_dump(mode="json"),
                )
                restored = _finish(admin, rollback)
                _ready(admin, source.manifest_sha256)
                _save(root, prefix + "-rollback.json", restored.model_dump(mode="json"))
        finally:
            # Revoking the initiating key while work remains active can invalidate
            # its authority. An observer failure must not force drain that way.
            settled = not submission_attempted
            if work_id is not None:
                settled = _work_settled(admin, route, work_id, key.secret)
            if settled:
                revoked = admin.delete("/api/v1/admin/keys/" + str(key.id))
                assert revoked.status_code in {204, 404}
            else:
                _save(
                    root,
                    prefix + "-pending-revocation.json",
                    {"key_id": str(key.id), "work_id": work_id, "reason": "work_not_settled"},
                )
