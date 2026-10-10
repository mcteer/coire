"""Uploaded preference API uses the same real-persistence authorization boundaries."""

import json
import uuid

import pytest
from sqlalchemy import select
from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.db import TrainingDatasetRevisionRow
from coire_core.models.preference import PreferenceSplitManifest


async def test_preference_upload_is_private_and_prompt_grouped(api: API) -> None:
    api.metadata["format"] = "preference"
    rows = [
        {"prompt": [{"role": "user", "content": prompt}], "chosen": "yes", "rejected": "no"}
        for prompt in ("a", "a", "b")
    ]
    response = await api.upload(b"\n".join(json.dumps(row).encode() for row in rows) + b"\n")
    assert response.status_code == 202 and response.json()["state"] == "analyzing"
    async with api.sessions() as session:
        dataset = await session.get(
            TrainingDatasetRevisionRow, uuid.UUID(response.json()["dataset_id"])
        )
        assert dataset is not None and dataset.format == "preference"
        split = PreferenceSplitManifest.model_validate(dataset.split_manifest)
        assert (1 in split.train_rows) == (2 in split.train_rows)
    detail = await api.client.get(
        f"/api/v1/admin/datasets/{response.json()['dataset_id']}", headers=api.headers
    )
    assert detail.status_code == 200 and detail.json()["row_count"] == 3
    assert "yes" not in detail.text and "storage_key" not in detail.text


@pytest.mark.parametrize(
    "bad",
    [
        {
            "prompt": [{"role": "user", "content": "a", "tools": []}],
            "chosen": "yes",
            "rejected": "no",
        },
        {"prompt": [{"role": "user", "content": "a"}], "chosen": "yes", "rejected": "yes"},
        {"prompt": [{"role": "assistant", "content": "a"}], "chosen": "yes", "rejected": "no"},
    ],
)
async def test_invalid_preference_content_has_safe_diagnostics(
    api: API, bad: dict[str, object]
) -> None:
    api.metadata["format"] = "preference"
    response = await api.upload(json.dumps(bad).encode() + b"\n")
    assert response.status_code == 202 and response.json()["state"] == "failed"
    async with api.sessions() as session:
        dataset = await session.scalar(select(TrainingDatasetRevisionRow))
        assert dataset is not None and dataset.invalid_count > 0
        assert "yes" not in str(dataset.diagnostics)
