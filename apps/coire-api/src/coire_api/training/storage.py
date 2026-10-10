"""Private, bounded uploaded JSONL source staging; never tokenizers or tensors on core."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from opentelemetry import metrics, trace
from pydantic import ValidationError

from coire_api.training.dataset_formats import normalize_row, split_digest, split_rows
from coire_core.errors import (
    TrainingConflict,
    TrainingQuotaExceeded,
    TrainingUploadTooLarge,
    TrainingValidationError,
)
from coire_core.models.datasets import (
    DatasetDiagnostic,
    DatasetFormat,
    DatasetUploadRequest,
    SplitManifest,
)
from coire_core.models.preference import PreferenceRow, PreferenceSplitManifest
from coire_core.preference_data import preference_split_digest, split_preference_hashes
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.api.training.datasets")
uploads = metrics.get_meter("coire.api.training").create_counter(
    "coire_dataset_sources_staged_total"
)
_FIELDS = frozenset(
    {
        "text",
        "prompt",
        "completion",
        "messages",
        "role",
        "content",
        "tool_calls",
        "function",
        "name",
        "arguments",
        "tool_call_id",
        "tools",
        "parameters",
        "type",
        "metadata",
        "parts",
        "id",
    }
)


@dataclass(frozen=True)
class StagedDataset:
    source_sha256: str
    source_bytes: int
    row_count: int
    invalid_count: int
    diagnostics: list[DatasetDiagnostic]
    split: SplitManifest | PreferenceSplitManifest | None

    @property
    def split_sha256(self) -> str | None:
        if isinstance(self.split, PreferenceSplitManifest):
            return preference_split_digest(self.split)
        return split_digest(self.split) if self.split else None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise ValueError("nonfinite JSON value")


def _field_path(location: tuple[str | int, ...]) -> str:
    result = ""
    for part in location:
        if isinstance(part, int):
            result += f"[{part}]"
        elif part in _FIELDS:
            result += ("." if result else "") + part
        else:
            break
        if len(result) > 200:
            return "row"
    return result or "row"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DatasetStore:
    """The caller must hold aggregate DB quota before receiving/spooling any body.

    This layer enforces actual bytes/rows, private generated paths and complete
    publication. It does not grant readiness, run analyses or mint input grants.
    """

    def __init__(self, settings: Settings) -> None:
        root = Path(settings.training_dataset_dir)
        if not root.is_absolute() or root.is_symlink():
            raise TrainingValidationError("Dataset storage must be a private contained directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077:
            raise TrainingValidationError("Dataset storage permissions must be private")
        self.root = root.resolve()
        self.settings = settings
        for child in (self.root / "staging", self.root / "sources"):
            if child.is_symlink():
                raise TrainingValidationError("Dataset storage subdirectories must not be linked")
            child.mkdir(mode=0o700, exist_ok=True)

    def _path(self, group: str, identity: uuid.UUID) -> Path:
        if not isinstance(identity, uuid.UUID):
            raise TrainingValidationError("Dataset storage requires a generated UUID")
        path = self.root / group / str(identity)
        if path.is_symlink():
            raise TrainingValidationError("Dataset storage path must not be linked")
        return path

    def stage_path(self, stage_id: uuid.UUID) -> Path:
        return self._path("staging", stage_id)

    def source_path(self, dataset_id: uuid.UUID) -> Path:
        return self._path("sources", dataset_id) / "source.jsonl"

    async def discard(self, stage_id: uuid.UUID) -> None:
        path = self.stage_path(stage_id)
        if path.exists():
            await anyio.to_thread.run_sync(shutil.rmtree, path)
        await anyio.to_thread.run_sync(_fsync_directory, path.parent)

    async def purge_source(self, dataset_id: uuid.UUID) -> None:
        """Remove only an internally selected retired revision, with durable proof."""
        path = self._path("sources", dataset_id)

        def remove() -> None:
            if path.exists():
                if not path.is_dir() or path.is_symlink():
                    raise TrainingValidationError("Dataset source directory is unsafe")
                shutil.rmtree(path)
            _fsync_directory(path.parent)

        await anyio.to_thread.run_sync(remove)

    async def retained_size(self, dataset_id: uuid.UUID) -> int:
        path = self._path("sources", dataset_id)

        def size() -> int:
            total = 0
            if not path.is_dir():
                raise TrainingConflict("Registered dataset source is missing")
            for item in path.iterdir():
                info = item.lstat()
                if not item.is_file() or item.is_symlink() or info.st_nlink != 1:
                    raise TrainingValidationError("Dataset source contains unsafe files")
                total += info.st_size
            return total

        return await anyio.to_thread.run_sync(size)

    async def discard_unpublished(self, dataset_id: uuid.UUID, expected_sha256: str) -> None:
        """Compensate only this request's uncommitted publication, never live-source deletion."""
        path = self.source_path(dataset_id)
        if not path.exists():
            return

        def remove() -> None:
            if path.is_symlink():
                raise TrainingValidationError("Unpublished source path is unsafe")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
                    raise TrainingConflict("Unpublished source ownership differs")
            shutil.rmtree(path.parent)
            _fsync_directory(path.parent.parent)

        await anyio.to_thread.run_sync(remove)

    async def stage(
        self,
        stage_id: uuid.UUID,
        dataset_id: uuid.UUID,
        metadata: DatasetUploadRequest,
        chunks: AsyncIterator[bytes],
        *,
        byte_ceiling: int,
    ) -> StagedDataset:
        if (
            type(byte_ceiling) is not int
            or not 1 <= byte_ceiling <= self.settings.training_dataset_upload_max_bytes
        ):
            raise TrainingUploadTooLarge()
        directory = self.stage_path(stage_id)
        if directory.exists():
            raise TrainingConflict("Dataset upload stage already exists")
        disk = await anyio.to_thread.run_sync(shutil.disk_usage, self.root)
        if disk.free - byte_ceiling < self.settings.training_dataset_disk_floor_bytes:
            raise TrainingQuotaExceeded("Dataset disk safety headroom is unavailable")
        await anyio.to_thread.run_sync(lambda: directory.mkdir(mode=0o700))
        path = directory / "source.jsonl"
        source_hash = hashlib.sha256()
        source_bytes = rows = invalid = 0
        diagnostics: list[DatasetDiagnostic] = []
        content_hashes: list[str] = []
        prompt_hashes: list[str] = []
        buffer = bytearray()
        oversized = False

        def invalid_row(code: str, field: str = "row") -> None:
            nonlocal invalid
            invalid += 1
            if len(diagnostics) < self.settings.training_diagnostic_max_rows:
                diagnostics.append(
                    DatasetDiagnostic.model_validate(
                        {"row": max(1, rows), "field": field, "code": code}
                    )
                )

        def finish_row() -> None:
            nonlocal rows, oversized
            rows += 1
            if rows > self.settings.training_dataset_max_rows:
                raise TrainingUploadTooLarge("Dataset row count exceeds its limit")
            if oversized:
                invalid_row("row_too_large")
            else:
                try:
                    text = buffer.decode("utf-8", errors="strict")
                    value = json.loads(
                        text, object_pairs_hook=_unique_object, parse_constant=_nonfinite
                    )
                    if metadata.format is DatasetFormat.PREFERENCE:
                        preference = PreferenceRow.model_validate(value)
                        content_hashes.append(preference.content_sha256())
                        prompt_hashes.append(preference.prompt_sha256())
                    else:
                        example = normalize_row(
                            value, format=metadata.format, dataset_id=dataset_id, source_row=rows
                        )
                        content_hashes.append(example.content_sha256())
                except UnicodeError:
                    invalid_row("invalid_utf8")
                except ValidationError as error:
                    errors = error.errors(include_context=False, include_url=False)
                    code = "invalid_schema"
                    if any(item.get("input") == "image_url" for item in errors):
                        code = "unsupported_image"
                    invalid_row(code, _field_path(errors[0]["loc"]))
                except (ValueError, RecursionError):
                    invalid_row("invalid_json")
            buffer.clear()
            oversized = False

        def consume(data: bytes) -> None:
            nonlocal oversized
            position = 0
            while position < len(data):
                newline = data.find(b"\n", position)
                end = len(data) if newline < 0 else newline
                if not oversized:
                    if len(buffer) + end - position > self.settings.training_dataset_row_max_bytes:
                        oversized = True
                        buffer.clear()
                    else:
                        buffer.extend(data[position:end])
                if newline >= 0:
                    finish_row()
                position = end + 1

        with tracer.start_as_current_span(
            "coire.api.training.dataset.stage",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("coire.dataset_id", str(dataset_id))
            try:
                async with await anyio.open_file(path, "xb") as stream:
                    await anyio.to_thread.run_sync(path.chmod, 0o600)
                    async for data in chunks:
                        if source_bytes + len(data) > byte_ceiling or len(data) > 1024**2:
                            raise TrainingUploadTooLarge()
                        source_bytes += len(data)
                        source_hash.update(data)
                        await stream.write(data)
                        await anyio.to_thread.run_sync(consume, data)
                    if buffer or oversized:
                        await anyio.to_thread.run_sync(finish_row)
                    await stream.flush()
                    descriptor = stream.wrapped.fileno()
                    await anyio.to_thread.run_sync(os.fsync, descriptor)
                split = None
                if rows == 0:
                    invalid_row("invalid_schema")
                if invalid == 0:
                    try:
                        split = await anyio.to_thread.run_sync(
                            lambda: (
                                split_preference_hashes(
                                    dataset_id,
                                    source_hash.hexdigest(),
                                    content_hashes,
                                    prompt_hashes,
                                    seed=metadata.split_seed,
                                    validation_fraction=metadata.validation_fraction,
                                )
                                if metadata.format is DatasetFormat.PREFERENCE
                                else split_rows(
                                    dataset_id,
                                    source_hash.hexdigest(),
                                    content_hashes,
                                    seed=metadata.split_seed,
                                    validation_fraction=metadata.validation_fraction,
                                )
                            )
                        )
                    except TrainingValidationError:
                        invalid_row("invalid_schema")
                await anyio.to_thread.run_sync(_fsync_directory, directory)
                uploads.add(1, {"state": "invalid" if invalid else "validated"})
                return StagedDataset(
                    source_hash.hexdigest(), source_bytes, rows, invalid, diagnostics, split
                )
            except BaseException:
                await self.discard(stage_id)
                raise

    async def commit(
        self, stage_id: uuid.UUID, dataset_id: uuid.UUID, result: StagedDataset
    ) -> int:
        if result.invalid_count or result.split is None or result.split.dataset_id != dataset_id:
            raise TrainingValidationError("Only a completely validated source may be committed")
        stage = self.stage_path(stage_id)
        destination = self._path("sources", dataset_id)
        if destination.exists():
            raise TrainingConflict("Dataset source identity is immutable")
        encoded = result.split.model_dump_json().encode()

        def publish() -> int:
            source = stage / "source.jsonl"
            if (
                source.is_symlink()
                or not source.is_file()
                or source.stat().st_size != result.source_bytes
            ):
                raise TrainingValidationError("Dataset source stage is absent or unsafe")
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != result.source_sha256:
                raise TrainingValidationError("Dataset staged bytes differ from validation")
            manifest = stage / "split.json"
            descriptor = os.open(
                manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(stage)
            os.rename(stage, destination)
            _fsync_directory(stage.parent)
            _fsync_directory(destination.parent)
            return result.source_bytes + len(encoded)

        return await anyio.to_thread.run_sync(publish)
