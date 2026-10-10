"""Private upload validation preserves both answers and prompt-group membership."""

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from coire_api.training.storage import DatasetStore
from coire_core.models.datasets import DatasetUploadRequest
from coire_core.models.preference import PreferenceSplitManifest
from coire_core.settings import Settings


async def test_preference_upload_stages_groups_and_refuses_identical_answers(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _secrets_dir="/nonexistent",
        training_dataset_dir=str(tmp_path / "private"),
        training_dataset_disk_floor_bytes=2 * 1024**3,
    )  # type: ignore[call-arg]
    store = DatasetStore(settings)
    metadata = DatasetUploadRequest.model_validate(
        {
            "name": "Preference fixture",
            "format": "preference",
            "provenance": {"source": "synthetic", "license_note": "Synthetic test data"},
            "analysis_model_id": str(uuid.uuid4()),
            "analysis_variant_id": str(uuid.uuid4()),
        }
    )
    rows = [
        {"prompt": [{"role": "user", "content": prompt}], "chosen": "yes", "rejected": "no"}
        for prompt in ("a", "a", "b")
    ]

    async def chunks() -> AsyncIterator[bytes]:
        yield b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"

    identity = uuid.uuid4()
    staged = await store.stage(uuid.uuid4(), identity, metadata, chunks(), byte_ceiling=1024)
    assert staged.invalid_count == 0 and isinstance(staged.split, PreferenceSplitManifest)
    assert (1 in staged.split.train_rows) == (2 in staged.split.train_rows)
    rows[0]["rejected"] = "yes"
    invalid = await store.stage(uuid.uuid4(), uuid.uuid4(), metadata, chunks(), byte_ceiling=1024)
    assert invalid.invalid_count == 1 and invalid.split is None
    assert "yes" not in str(invalid.diagnostics)


def test_streaming_copy_can_validate_identical_prefix_before_terminal() -> None:
    from coire_api.feedback.quota import feedback_copy_bytes

    prompt: list[dict[str, object]] = [{"role": "user", "content": "hello"}]
    assert feedback_copy_bytes(prompt, "same", "same", allow_identical=True) > 0
