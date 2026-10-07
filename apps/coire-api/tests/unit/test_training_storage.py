"""Real private JSONL staging, complete-row validation and atomic source/split publication."""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from coire_api.training.storage import DatasetStore
from coire_core.errors import TrainingUploadTooLarge, TrainingValidationError
from coire_core.models.datasets import DatasetFormat, DatasetProvenance, DatasetUploadRequest
from coire_core.settings import Settings


def metadata(format: DatasetFormat = DatasetFormat.TEXT) -> DatasetUploadRequest:
    return DatasetUploadRequest(
        name="Synthetic corpus",
        format=format,
        provenance=DatasetProvenance(source="synthetic", license_note="Synthetic test data"),
        analysis_model_id=uuid.uuid4(),
        analysis_variant_id=uuid.uuid4(),
    )


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    # Split JSON syntax and Unicode sequences across transport chunks deliberately.
    for offset in range(0, len(data), 7):
        yield data[offset : offset + 7]


@pytest.mark.parametrize(
    "format,rows",
    [
        (DatasetFormat.TEXT, [{"text": "one ☃"}, {"text": "two"}]),
        (
            DatasetFormat.PROMPT_COMPLETION,
            [{"prompt": "one", "completion": "1"}, {"prompt": "two", "completion": "2"}],
        ),
        (
            DatasetFormat.CONVERSATION,
            [
                {
                    "messages": [
                        {"role": "user", "content": "one"},
                        {"role": "assistant", "content": "1"},
                    ]
                },
                {
                    "messages": [
                        {"role": "user", "content": "two"},
                        {"role": "assistant", "content": "2"},
                    ]
                },
            ],
        ),
    ],
)
async def test_supported_sources_publish_only_after_full_validation(
    tmp_path: Path, format: DatasetFormat, rows: list[dict[str, object]]
) -> None:
    store = DatasetStore(Settings(training_dataset_dir=str(tmp_path / "private")))
    dataset_id, stage_id = uuid.uuid4(), uuid.uuid4()
    data = b"\n".join(json.dumps(row, ensure_ascii=False).encode() for row in rows)
    result = await store.stage(
        stage_id, dataset_id, metadata(format), chunks(data), byte_ceiling=len(data)
    )
    assert result.source_sha256 == hashlib.sha256(data).hexdigest()
    assert result.source_bytes == len(data) and result.row_count == 2
    assert result.invalid_count == 0 and result.split is not None
    assert not store.source_path(dataset_id).exists()
    await store.commit(stage_id, dataset_id, result)
    assert store.source_path(dataset_id).read_bytes() == data
    assert store.source_path(dataset_id).stat().st_mode & 0o077 == 0
    assert set(result.split.train_rows) | set(result.split.validation_rows) == {1, 2}


async def test_invalid_rows_are_counted_completely_without_echoing_data(tmp_path: Path) -> None:
    store = DatasetStore(
        Settings(training_dataset_dir=str(tmp_path), training_diagnostic_max_rows=2)
    )
    data = b'{"text":"valid"}\n{"private-secret-field":"never-echo-me"}\ninvalid-json\n\xff\n'
    stage_id, dataset_id = uuid.uuid4(), uuid.uuid4()
    result = await store.stage(
        stage_id, dataset_id, metadata(), chunks(data), byte_ceiling=len(data)
    )
    assert result.invalid_count == 3 and result.row_count == 4
    assert len(result.diagnostics) == 2 and result.split is None
    assert "private-secret-field" not in str(result.diagnostics)
    assert "never-echo-me" not in str(result.diagnostics)
    await store.discard(stage_id)
    assert not store.stage_path(stage_id).exists()
    assert not store.source_path(dataset_id).exists()


async def test_declared_and_actual_byte_bounds_fail_closed(tmp_path: Path) -> None:
    store = DatasetStore(
        Settings(
            training_dataset_dir=str(tmp_path),
            training_dataset_upload_max_bytes=32,
            training_dataset_row_max_bytes=32,
        )
    )
    stage_id = uuid.uuid4()
    data = b'{"text":"' + b"x" * 50 + b'"}\n'
    with pytest.raises(TrainingUploadTooLarge):
        await store.stage(stage_id, uuid.uuid4(), metadata(), chunks(data), byte_ceiling=32)
    assert not store.stage_path(stage_id).exists()


async def test_oversized_row_is_diagnosed_without_an_unbounded_line_buffer(tmp_path: Path) -> None:
    store = DatasetStore(
        Settings(training_dataset_dir=str(tmp_path), training_dataset_row_max_bytes=32)
    )
    data = b'{"text":"' + b"x" * 80 + b'"}\n{"text":"short"}\n'
    result = await store.stage(
        uuid.uuid4(), uuid.uuid4(), metadata(), chunks(data), byte_ceiling=len(data)
    )
    assert result.row_count == 2 and result.invalid_count == 1
    assert result.diagnostics[0].code == "row_too_large"


async def test_duplicate_groups_never_cross_split_and_metadata_does_not_change_content(
    tmp_path: Path,
) -> None:
    store = DatasetStore(Settings(training_dataset_dir=str(tmp_path)))
    rows = [
        {"text": "same", "metadata": {"note": "one"}},
        {"text": "same", "metadata": {"note": "two"}},
        {"text": "different"},
    ]
    data = b"\n".join(json.dumps(row).encode() for row in rows)
    result = await store.stage(
        uuid.uuid4(), uuid.uuid4(), metadata(), chunks(data), byte_ceiling=len(data)
    )
    assert result.split is not None
    assert ({1, 2} <= set(result.split.train_rows)) != ({1, 2} <= set(result.split.validation_rows))


async def test_transport_interruption_purges_private_partial_stage(tmp_path: Path) -> None:
    store = DatasetStore(Settings(training_dataset_dir=str(tmp_path)))
    stage_id = uuid.uuid4()

    async def interrupted() -> AsyncIterator[bytes]:
        yield b'{"text":"partial'
        raise ConnectionError("disconnected")

    with pytest.raises(ConnectionError):
        await store.stage(stage_id, uuid.uuid4(), metadata(), interrupted(), byte_ceiling=100)
    assert not store.stage_path(stage_id).exists()


def test_linked_store_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(TrainingValidationError):
        DatasetStore(Settings(training_dataset_dir=str(linked)))


async def test_changed_staged_bytes_cannot_be_published(tmp_path: Path) -> None:
    store = DatasetStore(Settings(training_dataset_dir=str(tmp_path)))
    stage_id, dataset_id = uuid.uuid4(), uuid.uuid4()
    data = b'{"text":"one"}\n{"text":"two"}\n'
    result = await store.stage(
        stage_id, dataset_id, metadata(), chunks(data), byte_ceiling=len(data)
    )
    source = store.stage_path(stage_id) / "source.jsonl"
    source.write_bytes(data.replace(b"one", b"bad"))
    with pytest.raises(TrainingValidationError, match="differ"):
        await store.commit(stage_id, dataset_id, result)
    assert not store.source_path(dataset_id).exists()


@pytest.mark.parametrize(
    "data",
    [
        b'{"text":"one","text":"two"}\n',
        b'{"text":"one","metadata":{"score":NaN}}\n',
        b"\n",
        b"",
    ],
)
async def test_ambiguous_nonfinite_and_empty_rows_never_produce_a_split(
    tmp_path: Path, data: bytes
) -> None:
    store = DatasetStore(Settings(training_dataset_dir=str(tmp_path)))
    result = await store.stage(
        uuid.uuid4(), uuid.uuid4(), metadata(), chunks(data), byte_ceiling=max(1, len(data))
    )
    assert result.invalid_count == 1 and result.split is None


async def test_image_rows_report_explicit_unsupported_image_code(tmp_path: Path) -> None:
    store = DatasetStore(Settings(training_dataset_dir=str(tmp_path)))
    data = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "private-data"}}],
                },
                {"role": "assistant", "content": "target"},
            ]
        }
    ).encode()
    result = await store.stage(
        uuid.uuid4(),
        uuid.uuid4(),
        metadata(DatasetFormat.CONVERSATION),
        chunks(data),
        byte_ceiling=len(data),
    )
    assert result.invalid_count == 1 and result.diagnostics[0].code == "unsupported_image"
    assert "private-data" not in str(result.diagnostics)
