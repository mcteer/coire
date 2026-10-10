"""Export admission is bounded, idempotent and independently cancellable."""

import uuid

from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.feedback.export_execution import prepare_export


async def test_export_admission_replay_conflict_and_cancel_with_gate_disabled(api: API) -> None:
    api.settings.preference_training_enabled = True
    url = "/api/v1/admin/feedback/exports"
    body = {
        "name": "private pairs",
        "license_note": "local consent",
        "model_id": api.metadata["analysis_model_id"],
        "variant_id": api.metadata["analysis_variant_id"],
    }
    headers = {**api.headers, "idempotency-key": str(uuid.uuid4())}
    first = await api.client.post(url, json=body, headers=headers)
    assert first.status_code == 202, first.text
    receipt = first.json()
    assert receipt["state"] == "queued"
    assert (await api.client.post(url, json=body, headers=headers)).json() == receipt
    assert (
        await api.client.post(url, json={**body, "name": "changed"}, headers=headers)
    ).status_code == 409
    detail = await api.client.get(url + "/" + receipt["id"], headers=api.headers)
    assert detail.status_code == 200 and detail.json()["selected_count"] == 0
    api.settings.preference_training_enabled = False
    api.settings.training_enabled = False
    cancelled = await api.client.post(
        url + "/" + receipt["id"] + "/cancel",
        json={"expected_version": 1},
        headers={**api.headers, "idempotency-key": "cancel"},
    )
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert (await api.client.get(url, headers=api.headers)).status_code == 200


async def test_export_default_gate_and_same_origin_are_enforced(api: API) -> None:
    url = "/api/v1/admin/feedback/exports"
    body = {
        "name": "pairs",
        "license_note": "local",
        "model_id": api.metadata["analysis_model_id"],
        "variant_id": api.metadata["analysis_variant_id"],
    }
    headers = {**api.headers, "idempotency-key": "admit"}
    assert (await api.client.post(url, json=body, headers=headers)).status_code == 503
    api.settings.preference_training_enabled = True
    assert (
        await api.client.post(url, json=body, headers={**headers, "origin": "https://other.test"})
    ).status_code == 403
    assert (await api.client.post(url, json=body)).status_code == 401


async def test_export_checks_analysis_and_filter_registry_parentage(api: API) -> None:
    api.settings.preference_training_enabled = True
    body = {
        "name": "pairs",
        "license_note": "local",
        "model_id": api.metadata["analysis_model_id"],
        "variant_id": str(uuid.uuid4()),
    }
    headers = {**api.headers, "idempotency-key": "invalid-analysis"}
    assert (
        await api.client.post("/api/v1/admin/feedback/exports", json=body, headers=headers)
    ).status_code == 422

    body["variant_id"] = api.metadata["analysis_variant_id"]
    body["filters"] = {
        "model_id": api.metadata["analysis_model_id"],
        "variant_id": str(uuid.uuid4()),
    }
    assert (
        await api.client.post(
            "/api/v1/admin/feedback/exports",
            json=body,
            headers={**api.headers, "idempotency-key": "invalid-filter"},
        )
    ).status_code == 422


async def test_empty_export_finishes_without_private_files(api: API) -> None:
    api.settings.preference_training_enabled = True
    response = await api.client.post(
        "/api/v1/admin/feedback/exports",
        headers={**api.headers, "idempotency-key": "empty"},
        json={
            "name": "empty",
            "license_note": "local",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        },
    )
    assert response.status_code == 202
    identity = response.json()["id"]
    assert await prepare_export(identity, api.settings) == "failed"
    detail = await api.client.get("/api/v1/admin/feedback/exports/" + identity, headers=api.headers)
    assert detail.json()["reason"] == "empty"
    assert detail.json()["dataset_id"] is None and not detail.json()["cleanup_pending"]


async def test_export_queue_accepts_one_hundred_and_rejects_next(api: API) -> None:
    from datetime import UTC, datetime, timedelta

    from coire_api.auth import Principal, PrincipalKind
    from coire_api.db import PreferenceExportRow
    from coire_api.training.service import training_id
    from coire_core.models.feedback import PreferenceExportCreate

    api.settings.preference_training_enabled = True
    body = PreferenceExportCreate.model_validate(
        {
            "name": "pairs",
            "license_note": "local",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        }
    )
    now = datetime.now(UTC)
    async with api.sessions() as session:
        session.add_all(
            [
                PreferenceExportRow(
                    id=training_id(),
                    owner_user_id=api.owner,
                    authorization_snapshot=Principal(
                        kind=PrincipalKind.ADMIN, user_id=api.owner
                    ).model_dump(mode="json"),
                    request=body.model_dump(mode="json"),
                    state="queued",
                    queue_deadline_at=now + timedelta(hours=1),
                )
                for _ in range(99)
            ]
        )
        await session.commit()
    url = "/api/v1/admin/feedback/exports"
    hundredth = await api.client.post(
        url,
        headers={**api.headers, "idempotency-key": "hundredth"},
        json=body.model_dump(mode="json"),
    )
    assert hundredth.status_code == 202
    refused = await api.client.post(
        url,
        headers={**api.headers, "idempotency-key": "hundred-first"},
        json=body.model_dump(mode="json"),
    )
    assert refused.status_code == 429
    replay = await api.client.post(
        url,
        headers={**api.headers, "idempotency-key": "hundredth"},
        json=body.model_dump(mode="json"),
    )
    assert replay.status_code == 202 and replay.json()["id"] == hundredth.json()["id"]
