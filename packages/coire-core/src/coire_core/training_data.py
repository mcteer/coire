"""Model-free uploaded-row normalization and exact-content splits shared with Studio analysis."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from coire_core.errors import TrainingValidationError
from coire_core.models.conversation import (
    Conversation,
    ConversationMessage,
    ConversationTool,
    ImagePart,
    TextPart,
    ToolCall,
)
from coire_core.models.datasets import (
    DatasetFormat,
    DatasetMixture,
    SftConversationRow,
    SftPromptCompletionRow,
    SftTextRow,
    SplitManifest,
    TrainingExample,
)


def normalize_row(
    value: object, *, format: DatasetFormat, dataset_id: uuid.UUID, source_row: int
) -> TrainingExample:
    if format is DatasetFormat.PREFERENCE:
        raise TrainingValidationError("Preference rows require paired normalization")
    conversation_id = uuid.uuid5(dataset_id, f"row:{source_row}")
    if format is DatasetFormat.TEXT:
        row = SftTextRow.model_validate(value)
        conversation = Conversation(
            id=conversation_id,
            messages=[
                ConversationMessage(
                    id=uuid.uuid5(conversation_id, "message:0"),
                    role="assistant",
                    parts=[TextPart(text=row.text)],
                )
            ],
            metadata=row.metadata,
        )
        return TrainingExample(
            conversation=conversation,
            source_row=source_row,
            content_mode="text",
            loss_policy="all_tokens",
        )
    if format is DatasetFormat.PROMPT_COMPLETION:
        pair = SftPromptCompletionRow.model_validate(value)
        conversation = Conversation(
            id=conversation_id,
            messages=[
                ConversationMessage(
                    id=uuid.uuid5(conversation_id, "message:0"),
                    role="user",
                    parts=[TextPart(text=pair.prompt)],
                ),
                ConversationMessage(
                    id=uuid.uuid5(conversation_id, "message:1"),
                    role="assistant",
                    parts=[TextPart(text=pair.completion)],
                ),
            ],
            metadata=pair.metadata,
        )
        return TrainingExample(conversation=conversation, source_row=source_row)
    chat_row = SftConversationRow.model_validate(value)
    messages = []
    for index, message in enumerate(chat_row.messages):
        parts: list[TextPart | ImagePart] = []
        if isinstance(message.content, str) and message.content:
            parts = [TextPart(text=message.content)]
        elif isinstance(message.content, list):
            parts = [TextPart(text=part.text) for part in message.content]
        messages.append(
            ConversationMessage(
                id=message.id or uuid.uuid5(conversation_id, f"message:{index}"),
                role=message.role,
                parts=parts,
                tool_calls=[
                    ToolCall.model_validate(
                        {
                            "id": call.id,
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        }
                    )
                    for call in message.tool_calls
                ],
                tool_call_id=message.tool_call_id,
                metadata=message.metadata,
            )
        )
    tools = [
        ConversationTool.model_validate(tool.function.model_dump(mode="json"))
        for tool in chat_row.tools
    ]
    conversation = Conversation(
        id=conversation_id, messages=messages, tools=tools, metadata=chat_row.metadata
    )
    return TrainingExample(conversation=conversation, source_row=source_row)


def split_rows(
    dataset_id: uuid.UUID,
    source_sha256: str,
    content_hashes: list[str],
    *,
    seed: int,
    validation_fraction: float,
) -> SplitManifest:
    if not 0 < validation_fraction < 1:
        raise TrainingValidationError("Dataset validation fraction is invalid")
    groups: dict[str, list[int]] = defaultdict(list)
    for row, digest in enumerate(content_hashes, start=1):
        groups[digest].append(row)
    if len(groups) < 2:
        raise TrainingValidationError(
            "Dataset needs at least two distinct content groups for nonempty train/validation splits"
        )
    ordered = sorted(
        groups,
        key=lambda digest: hashlib.sha256(f"coire-split-v1:{seed}:{digest}".encode()).digest(),
    )
    target = max(1, round(len(content_hashes) * validation_fraction))
    selected: set[str] = set()
    count = 0
    for digest in ordered[:-1]:
        selected.add(digest)
        count += len(groups[digest])
        if count >= target:
            break
    validation = sorted(row for digest in selected for row in groups[digest])
    train = sorted(row for digest in groups if digest not in selected for row in groups[digest])
    return SplitManifest(
        dataset_id=dataset_id,
        source_sha256=source_sha256,
        seed=seed,
        train_rows=train,
        validation_rows=validation,
        row_content_sha256=content_hashes,
    )


def split_digest(manifest: SplitManifest) -> str:
    data = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CompiledMixtureSource:
    dataset_id: uuid.UUID
    source_sha256: str
    split_sha256: str
    rows: tuple[int, ...]
    quota: int


@dataclass(frozen=True)
class CompiledMixture:
    """Internal model-free compilation result, not a wire contract."""

    sources: tuple[CompiledMixtureSource, ...]
    identity_sha256: str
    seed: int
    strategy: Literal["weighted", "sequential"]
    replacement: bool
    epoch_samples: int


def largest_remainder_quotas(proportions: Sequence[float], total: int) -> tuple[int, ...]:
    """Normalize decimal intent exactly; source order breaks equal remainder ties."""
    if type(total) is not int or not 1 <= total <= 16_000_000 or not 1 <= len(proportions) <= 16:
        raise TrainingValidationError("Mixture quota bounds are invalid")
    try:
        weights = [Fraction(str(value)) for value in proportions]
    except (ValueError, ZeroDivisionError):
        raise TrainingValidationError("Mixture proportions must be finite and positive") from None
    if any(weight <= 0 or weight > 1 for weight in weights):
        raise TrainingValidationError("Mixture proportions must be finite and positive")
    denominator = sum(weights, Fraction(0))
    if abs(denominator - 1) > Fraction(1, 1_000_000):
        raise TrainingValidationError("Mixture proportions must sum to one")
    exact = [total * weight / denominator for weight in weights]
    quotas = [value.numerator // value.denominator for value in exact]
    order = sorted(range(len(weights)), key=lambda index: (-(exact[index] - quotas[index]), index))
    for index in order[: total - sum(quotas)]:
        quotas[index] += 1
    return tuple(quotas)


def detect_split_leakage(manifests: Sequence[SplitManifest]) -> None:
    """Refuse exact-content conflicts across entire splits, including unselected rows."""
    train: set[str] = set()
    validation: set[str] = set()
    for manifest in manifests:
        train.update(manifest.row_content_sha256[row - 1] for row in manifest.train_rows)
        validation.update(manifest.row_content_sha256[row - 1] for row in manifest.validation_rows)
    if train & validation:
        raise TrainingValidationError(
            "Mixture sources contain conflicting duplicate split membership"
        )


def compile_mixture(
    mixture: DatasetMixture,
    manifests: Mapping[uuid.UUID, SplitManifest],
    *,
    validation_manifests: Sequence[SplitManifest] = (),
) -> CompiledMixture:
    """Compile ready authorized revisions; caller owns readiness and analysis checks."""
    mixture = DatasetMixture.model_validate(mixture.model_dump(mode="json"))
    selected = []
    for source in mixture.datasets:
        manifest = manifests.get(source.dataset_id)
        if manifest is None or manifest.dataset_id != source.dataset_id:
            raise TrainingValidationError("Mixture source split is unavailable or mismatched")
        selected.append(SplitManifest.model_validate(manifest.model_dump(mode="json")))
    held_out = [
        SplitManifest.model_validate(item.model_dump(mode="json")) for item in validation_manifests
    ]
    detect_split_leakage([*selected, *held_out])
    quotas = largest_remainder_quotas(
        [source.mixture_proportion for source in mixture.datasets], mixture.epoch_samples
    )
    compiled = []
    for source, manifest, quota in zip(mixture.datasets, selected, quotas, strict=True):
        if source.sample_count > len(manifest.train_rows):
            raise TrainingValidationError(
                "Mixture sample_count exceeds its immutable training pool"
            )
        if not mixture.replacement and quota > source.sample_count:
            raise TrainingValidationError(
                "Mixture source quota exceeds its pool without replacement"
            )
        compiled.append(
            CompiledMixtureSource(
                dataset_id=source.dataset_id,
                source_sha256=manifest.source_sha256,
                split_sha256=split_digest(manifest),
                rows=tuple(sorted(manifest.train_rows)[: source.sample_count]),
                quota=quota,
            )
        )
    payload = {
        "algorithm": "mixture-index-v1",
        "intent": mixture.model_dump(mode="json"),
        "splits": [item.split_sha256 for item in compiled],
        "validation_splits": sorted(split_digest(item) for item in held_out),
        "quotas": quotas,
    }
    identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CompiledMixture(
        tuple(compiled),
        identity,
        mixture.seed,
        mixture.mixture_strategy,
        mixture.replacement,
        mixture.epoch_samples,
    )
