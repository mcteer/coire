"""Studio-only CPU tokenizer analysis; no model construction or weight allocation."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import platform
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast

from opentelemetry import trace

from coire_core.errors import TrainingValidationError
from coire_core.models.datasets import (
    DatasetAnalysis,
    DatasetAnalysisBinding,
    DatasetDiagnostic,
    DatasetFormat,
    TokenDistribution,
    TokenizedTrainingExample,
)
from coire_core.models.jobs import ChecksumManifest
from coire_core.models.preference import (
    PreferenceAnalysis,
    PreferenceRow,
    PreferenceTokenSummary,
    canonical_bytes,
)
from coire_core.models.training_node import DatasetAnalysisWorkerInput, NodeDatasetAnalysisRequest
from coire_core.training_data import normalize_row
from coire_node.training.rendering import (
    ChatTokenizer,
    render_preference_example,
    supervision_tokens,
)

tracer = trace.get_tracer("coire.node.training.datasets")


@dataclass(frozen=True)
class IndexedTrainingSource:
    """Private immutable row-index view of one verified token cache (no merged corpus)."""

    dataset_id: uuid.UUID
    source_sha256: str
    split_sha256: str
    rows: tuple[int, ...]
    examples: tuple[TokenizedTrainingExample, ...]
    quota: int


def index_training_source(
    *,
    dataset_id: uuid.UUID,
    source_sha256: str,
    split_sha256: str,
    rows: tuple[int, ...],
    cache: Mapping[int, TokenizedTrainingExample],
    quota: int,
) -> IndexedTrainingSource:
    """Bind compiled one-based train rows to independently verified Studio cache entries."""
    if (
        not 1 <= len(rows) <= 1_000_000
        or len(set(rows)) != len(rows)
        or any(type(row) is not int or row < 1 for row in rows)
        or type(quota) is not int
        or not 0 <= quota <= 16_000_000
        or any(
            len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            for digest in (source_sha256, split_sha256)
        )
    ):
        raise TrainingValidationError("Mixture source index is invalid")
    examples = []
    for row in rows:
        example = cache.get(row)
        if example is None or example.source_row != row:
            raise TrainingValidationError("Mixture source token cache is incomplete or mismatched")
        examples.append(TokenizedTrainingExample.model_validate(example.model_dump(mode="json")))
    return IndexedTrainingSource(
        dataset_id, source_sha256, split_sha256, rows, tuple(examples), quota
    )


def distribution(lengths: list[int]) -> TokenDistribution | None:
    if not lengths:
        return None
    values = sorted(lengths)
    upper_bounds = [2**power for power in range(1, 22)]
    while upper_bounds[-1] < values[-1]:
        upper_bounds.append(upper_bounds[-1] * 2)
    histogram = [0] * len(upper_bounds)
    for value in values:
        histogram[bisect.bisect_left(upper_bounds, value)] += 1
    return TokenDistribution(
        minimum=values[0],
        maximum=values[-1],
        p50=values[max(0, math.ceil(0.5 * len(values)) - 1)],
        p95=values[max(0, math.ceil(0.95 * len(values)) - 1)],
        histogram=histogram,
        upper_bounds=upper_bounds,
    )


def analyze_source(
    source: Path,
    command: NodeDatasetAnalysisRequest | DatasetAnalysisWorkerInput,
    tokenizer: ChatTokenizer,
    *,
    tokenizer_sha256: str,
    template_sha256: str,
    runtime_sha256: str,
    max_sequence_length: int = 8192,
    diagnostic_limit: int = 100,
) -> DatasetAnalysis:
    """Streaming statistics use shared serialization/serving primitives and never truncate."""
    expected_bytes = (
        command.source_bytes
        if isinstance(command, DatasetAnalysisWorkerInput)
        else command.input_grant.max_bytes
    )
    if source.is_symlink() or not source.is_file() or source.stat().st_size != expected_bytes:
        raise TrainingValidationError("Dataset analysis source bytes are unavailable")
    with source.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != command.binding.source_sha256:
            raise TrainingValidationError(
                "Dataset source differs from its immutable analysis binding"
            )
    if command.binding.format is DatasetFormat.PREFERENCE:
        return analyze_preference_source(
            source,
            command,
            tokenizer,
            tokenizer_sha256=tokenizer_sha256,
            template_sha256=template_sha256,
            runtime_sha256=runtime_sha256,
            max_sequence_length=max_sequence_length,
            diagnostic_limit=diagnostic_limit,
        )
    lengths: list[int] = []
    roles: Counter[Literal["system", "user", "assistant", "tool"]] = Counter()
    content_hashes: Counter[str] = Counter()
    diagnostics: list[DatasetDiagnostic] = []
    invalid = 0
    if not 0 <= diagnostic_limit <= 100:
        raise TrainingValidationError("Invalid analysis diagnostic bound")
    with tracer.start_as_current_span("coire.node.training.dataset.analyze") as span:
        span.set_attribute("coire.dataset_id", str(command.binding.dataset_id))
        with source.open("rb") as stream:
            row = 0
            while data := stream.readline(1024**2 + 2):
                row += 1
                if row > 1_000_000 or len(data.rstrip(b"\n")) > 1024**2:
                    raise TrainingValidationError("Dataset source exceeds the analysis row bounds")
                if datetime.now(UTC) >= command.deadline:
                    raise TrainingValidationError("Dataset analysis deadline expired")
                code = None
                try:
                    example = normalize_row(
                        json.loads(data),
                        format=command.binding.format,
                        dataset_id=command.binding.dataset_id,
                        source_row=row,
                    )
                    content_hashes[example.content_sha256()] += 1
                    roles.update(message.role for message in example.conversation.messages)
                    tokens, _start = supervision_tokens(
                        example,
                        tokenizer,
                        max_sequence_length=max_sequence_length,
                        enable_thinking=command.binding.enable_thinking,
                    )
                    lengths.append(len(tokens))
                    if len(tokens) > max_sequence_length:
                        code = "overlength"
                except TrainingValidationError as error:
                    code = (
                        "zero_target"
                        if "supervised target" in error.detail
                        else "template_incompatible"
                    )
                except (ValueError, UnicodeError, RecursionError):
                    code = "invalid_schema"
                if code:
                    invalid += 1
                    if len(diagnostics) < diagnostic_limit:
                        diagnostics.append(
                            DatasetDiagnostic.model_validate(
                                {"row": row, "field": "row", "code": code}
                            )
                        )
        if not row:
            raise TrainingValidationError("Dataset analysis source is empty")
        return DatasetAnalysis(
            id=command.analysis_id,
            dataset_id=command.binding.dataset_id,
            model_id=command.binding.model_id,
            variant_id=command.binding.variant_id,
            tokenizer_sha256=tokenizer_sha256,
            template_sha256=template_sha256,
            runtime_sha256=runtime_sha256,
            state="failed" if invalid else "succeeded",
            tokens=distribution(lengths),
            role_counts=dict(roles),
            duplicate_rows=sum(count - 1 for count in content_hashes.values()),
            invalid_count=invalid,
            row_count=row,
            diagnostics=diagnostics,
            created_at=datetime.now(UTC),
        )


def load_analysis_tokenizer(
    model_path: Path, binding: DatasetAnalysisBinding
) -> tuple[ChatTokenizer, str, str, str]:
    # Training keeps its established architecture and 64 KiB configuration gate.
    # Evaluation only reads the inert tokenizer for an already acquired serving
    # target; it must not inherit the narrower supported SFT model matrix.
    if platform.node().lower().split(".", 1)[0] == "coire-core" or platform.system() != "Darwin":
        raise TrainingValidationError("Tokenizer analysis is restricted to Studios")
    from coire_node.training.objectives import APPROVED_ARCHITECTURES, _read_config

    if not model_path.is_absolute() or model_path.is_symlink() or not model_path.is_dir():
        raise TrainingValidationError("Analysis requires a local acquired tokenizer")
    config = _read_config(model_path / "config.json")
    if config.get("model_type") not in APPROVED_ARCHITECTURES:
        raise TrainingValidationError("Executable or unapproved analysis tokenizer configuration")
    return load_evaluation_tokenizer(
        model_path,
        base_manifest_sha256=binding.base_manifest_sha256,
        template_override=binding.template_override,
    )


def _evaluation_model_config(path: Path) -> dict[str, object]:
    """Bounded inert serving metadata; never a model loader or executable config."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024**2:
        raise TrainingValidationError("Evaluation model configuration is missing or oversized")
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError, RecursionError):
        raise TrainingValidationError("Evaluation model configuration is invalid") from None
    if not isinstance(value, dict):
        raise TrainingValidationError("Evaluation model configuration must be an object")
    return value


def load_evaluation_tokenizer(
    model_path: Path, *, base_manifest_sha256: str, template_override: str | None
) -> tuple[ChatTokenizer, str, str, str]:
    """Verified inert assets and built-in tokenizer classes only; no mlx-lm model loader."""
    if platform.node().lower().split(".", 1)[0] == "coire-core" or platform.system() != "Darwin":
        raise TrainingValidationError("Tokenizer analysis is restricted to Studios")
    from coire_node.training.objectives import (
        APPROVED_TOKENIZERS,
        APPROVED_TOOL_PARSERS,
        _read_config,
    )

    if not model_path.is_absolute() or model_path.is_symlink() or not model_path.is_dir():
        raise TrainingValidationError("Analysis requires a local acquired tokenizer")
    manifest_path = model_path.with_name(model_path.name + ".manifest.json")
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > 4 * 1024**2
    ):
        raise TrainingValidationError("Analysis acquisition manifest unavailable")
    manifest = ChecksumManifest.model_validate_json(manifest_path.read_bytes())
    if (
        manifest.slug != model_path.name
        or manifest.total_bytes != sum(entry.bytes for entry in manifest.files)
        or hashlib.sha256(manifest.canonical_bytes()).hexdigest() != base_manifest_sha256
    ):
        raise TrainingValidationError("Analysis base manifest differs from registry binding")
    listed = {entry.path: entry for entry in manifest.files}
    if (
        len(listed) != len(manifest.files)
        or not {"config.json", "tokenizer_config.json"} <= listed.keys()
        or not ({"tokenizer.json", "tokenizer.model"} & listed.keys())
    ):
        raise TrainingValidationError("Analysis tokenizer manifest incomplete")
    # Check inert tokenizer/config bytes only: analysis never opens model weights.
    for entry in manifest.files:
        relative = Path(entry.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise TrainingValidationError("Analysis manifest path is unsafe")
        if not (
            entry.path.startswith(
                ("tokenizer", "vocab", "merges", "special_tokens", "added_tokens", "chat_template")
            )
            or entry.path == "config.json"
        ):
            continue
        path = model_path / relative
        if (
            any(parent.is_symlink() for parent in (path, *path.parents))
            or not path.is_file()
            or path.stat().st_size != entry.bytes
            or entry.bytes > 256 * 1024**2
        ):
            raise TrainingValidationError("Analysis tokenizer asset is unsafe")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != entry.sha256:
                raise TrainingValidationError("Analysis tokenizer checksum differs")
    for path in model_path.rglob("*"):
        if path.is_symlink() or (
            path.is_file()
            and path.relative_to(model_path).as_posix() not in listed
            and ".cache" not in path.relative_to(model_path).parts
        ):
            raise TrainingValidationError("Analysis model tree has unacquired assets")
    config = _evaluation_model_config(model_path / "config.json")
    token_config = _read_config(model_path / "tokenizer_config.json")
    if (
        config.get("model_file") is not None
        or config.get("auto_map")
        or not isinstance(config.get("model_type"), str)
        or token_config.get("auto_map")
        or not isinstance(token_config.get("tokenizer_class"), str)
        or token_config.get("tokenizer_class") not in APPROVED_TOKENIZERS
        or token_config.get("chat_template_type")
        or (
            token_config.get("tool_parser_type") is not None
            and (
                not isinstance(token_config.get("tool_parser_type"), str)
                or token_config.get("tool_parser_type") not in APPROVED_TOOL_PARSERS
            )
        )
    ):
        raise TrainingValidationError("Executable or unapproved analysis tokenizer configuration")

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "MLX_TRUST_REMOTE_CODE"):
        os.environ.pop(name, None)

    from mlx_lm.tokenizer_utils import load

    tokenizer = load(
        model_path, tokenizer_config_extra={"trust_remote_code": False, "local_files_only": True}
    )
    if template_override is not None:
        tokenizer.chat_template = template_override
    template = tokenizer.chat_template
    if not isinstance(template, str) or not template:
        raise TrainingValidationError(
            "Dataset analysis requires a supported effective chat template"
        )
    tokenizer_files = sorted(
        (item.path, item.sha256)
        for item in manifest.files
        if item.path.startswith(("tokenizer", "vocab", "merges", "special_tokens", "added_tokens"))
    )
    tokenizer_sha = hashlib.sha256(
        json.dumps(tokenizer_files, separators=(",", ":")).encode()
    ).hexdigest()
    template_sha = hashlib.sha256(template.encode()).hexdigest()
    runtime_sha = hashlib.sha256(
        json.dumps(
            {name: version(name) for name in ("mlx", "mlx-lm", "transformers", "tokenizers")},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return cast(ChatTokenizer, tokenizer), tokenizer_sha, template_sha, runtime_sha


def analyze_preference_source(
    source: Path,
    command: NodeDatasetAnalysisRequest | DatasetAnalysisWorkerInput,
    tokenizer: ChatTokenizer,
    *,
    tokenizer_sha256: str,
    template_sha256: str,
    runtime_sha256: str,
    max_sequence_length: int,
    diagnostic_limit: int,
) -> DatasetAnalysis:
    chosen_counts: list[int] = []
    rejected_counts: list[int] = []
    response_counts: list[tuple[int, int]] = []
    token_rows_hash = hashlib.sha256()
    groups: set[str] = set()
    hashes: Counter[str] = Counter()
    roles: Counter[Literal["system", "user", "assistant", "tool"]] = Counter()
    diagnostics: list[DatasetDiagnostic] = []
    invalid = 0
    row_count = 0
    if not 0 <= diagnostic_limit <= 100:
        raise TrainingValidationError("Invalid analysis diagnostic bound")
    with tracer.start_as_current_span("coire.node.training.preference.analyze"):
        with source.open("rb") as stream:
            while data := stream.readline(256 * 1024 + 2):
                row_count += 1
                if row_count > 1_000_000 or len(data.rstrip(b"\n")) > 256 * 1024:
                    raise TrainingValidationError("Preference source exceeds analysis bounds")
                if datetime.now(UTC) >= command.deadline:
                    raise TrainingValidationError("Preference analysis deadline expired")
                code = None
                try:
                    example = PreferenceRow.model_validate(json.loads(data))
                    tokenized = render_preference_example(
                        example,
                        tokenizer,
                        source_row=row_count,
                        max_sequence_length=max_sequence_length,
                        enable_thinking=command.binding.enable_thinking,
                    )
                    token_rows_hash.update(canonical_bytes(tokenized.model_dump(mode="json")))
                    token_rows_hash.update(b"\n")
                    chosen_counts.append(len(tokenized.chosen_tokens))
                    rejected_counts.append(len(tokenized.rejected_tokens))
                    response_counts.append(
                        (sum(tokenized.chosen_mask), sum(tokenized.rejected_mask))
                    )
                    groups.add(example.prompt_sha256())
                    hashes[example.content_sha256()] += 1
                    roles.update(message.role for message in example.prompt)
                    roles["assistant"] += 2
                except TrainingValidationError as error:
                    code = (
                        "overlength"
                        if "sequence length" in error.detail
                        else "zero_target"
                        if "supervised target" in error.detail
                        else "token_identical"
                        if "token-identical" in error.detail
                        else "template_incompatible"
                    )
                except (ValueError, UnicodeError, RecursionError):
                    code = "invalid_schema"
                if code:
                    invalid += 1
                    if len(diagnostics) < diagnostic_limit:
                        diagnostics.append(
                            DatasetDiagnostic.model_validate(
                                {"row": row_count, "field": "row", "code": code}
                            )
                        )
        if not row_count:
            raise TrainingValidationError("Preference source is empty")
        if len(groups) < 2 and not invalid:
            invalid += 1
            if len(diagnostics) < diagnostic_limit:
                diagnostics.append(
                    DatasetDiagnostic.model_validate(
                        {"row": 1, "field": "prompt", "code": "invalid_schema"}
                    )
                )
        duplicates = sum(count - 1 for count in hashes.values())
        preference = (
            None
            if invalid
            else PreferenceAnalysis(
                dataset_id=command.binding.dataset_id,
                source_sha256=command.binding.source_sha256,
                split_sha256=command.binding.split_sha256,
                tokenizer_sha256=tokenizer_sha256,
                template_sha256=template_sha256,
                runtime_sha256=runtime_sha256,
                row_count=row_count,
                prompt_group_count=len(groups),
                token_rows_sha256=token_rows_hash.hexdigest(),
                chosen_tokens=preference_token_summary(chosen_counts),
                rejected_tokens=preference_token_summary(rejected_counts),
                chosen_response_tokens=preference_token_summary(
                    [pair[0] for pair in response_counts]
                ),
                rejected_response_tokens=preference_token_summary(
                    [pair[1] for pair in response_counts]
                ),
                duplicate_rows=duplicates,
            )
        )
        return DatasetAnalysis(
            id=command.analysis_id,
            dataset_id=command.binding.dataset_id,
            model_id=command.binding.model_id,
            variant_id=command.binding.variant_id,
            tokenizer_sha256=tokenizer_sha256,
            template_sha256=template_sha256,
            runtime_sha256=runtime_sha256,
            state="failed" if invalid else "succeeded",
            tokens=distribution(chosen_counts + rejected_counts),
            role_counts=dict(roles),
            duplicate_rows=duplicates,
            invalid_count=invalid,
            row_count=row_count,
            diagnostics=diagnostics,
            preference=preference,
            created_at=datetime.now(UTC),
        )


def preference_token_summary(values: list[int]) -> PreferenceTokenSummary:
    return PreferenceTokenSummary(minimum=min(values), maximum=max(values), total=sum(values))
