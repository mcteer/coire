"""Bounded inert YAML parsing, verbatim provenance and canonical client intent."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Hashable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode

from coire_core.errors import TrainingUploadTooLarge, TrainingValidationError
from coire_core.models.training import (
    ParsedTrainingSubmission,
    TrainingReason,
    TrainingRecipePage,
    TrainingSpec,
    TrainingSubmission,
    TrainingValidation,
)
from coire_core.settings import Settings, get_settings

_TAGS = frozenset(
    "tag:yaml.org,2002:" + name for name in ("str", "int", "float", "bool", "null", "map", "seq")
)
_SCHEMA = TrainingSpec.model_json_schema()
_FIELD_NAMES = frozenset(_SCHEMA.get("properties", {})) | frozenset(
    name for schema in _SCHEMA.get("$defs", {}).values() for name in schema.get("properties", {})
)


class RecipeShapeError(ValueError):
    pass


class RecipeLoader(yaml.SafeLoader):
    """Never alter the global loader used by other services or accept YAML object tags."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        if not isinstance(node, MappingNode):
            raise RecipeShapeError("Recipe mapping is malformed")
        result: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise RecipeShapeError("Recipe mapping keys must be strings")
            if key in result:
                raise RecipeShapeError("Recipe contains a duplicate mapping key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


# YAML's safe float constructor already handles exponent notation; its default resolver
# requires a decimal point. Support the documented unquoted learning_rate: 1e-5 locally.
RecipeLoader.yaml_implicit_resolvers = {
    key: list(value) for key, value in RecipeLoader.yaml_implicit_resolvers.items()
}
RecipeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?[0-9][0-9_]*(?:\.[0-9_]*)?[eE][-+]?[0-9]+$"),
    list("-+0123456789"),
)


def _bounded_source(source: str, settings: Settings) -> bytes:
    try:
        encoded = source.encode("utf-8", errors="strict")
    except UnicodeError:
        raise TrainingValidationError("Recipe must be valid UTF-8") from None
    if len(encoded) > settings.training_recipe_max_bytes:
        raise TrainingUploadTooLarge()
    if not encoded or "\x00" in source:
        raise TrainingValidationError("Recipe must contain valid nonempty YAML")
    return encoded


def _check_structure(source: str, settings: Settings) -> None:
    depth = 0
    for count, event in enumerate(yaml.parse(source, Loader=RecipeLoader), start=1):
        if count > 32768:
            raise RecipeShapeError("Recipe exceeds its structural element bound")
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise RecipeShapeError("Recipe aliases and anchors are unsupported")
        tag = getattr(event, "tag", None)
        if tag is not None and tag not in _TAGS:
            raise RecipeShapeError("Recipe contains an unsupported YAML tag")
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            depth += 1
            if depth > settings.training_yaml_max_depth:
                raise RecipeShapeError("Recipe exceeds its depth bound")
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            depth -= 1


def _safe_fields(error: ValidationError) -> str:
    locations: list[str] = []
    for item in error.errors(include_input=False, include_context=False)[:20]:
        parts = [
            str(part) if isinstance(part, int) or part in _FIELD_NAMES else "unknown_field"
            for part in item["loc"]
        ]
        path = ".".join(parts) or "recipe"
        if path not in locations:
            locations.append(path)
    return ", ".join(locations)


def parse_spec(source: str, *, settings: Settings | None = None) -> TrainingSpec:
    config = settings or get_settings()
    _bounded_source(source, config)
    try:
        _check_structure(source, config)
        value = yaml.load(source, Loader=RecipeLoader)
    except RecipeShapeError as error:
        raise TrainingValidationError(str(error)) from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise TrainingValidationError(f"Recipe YAML syntax is invalid{location}") from None
    if not isinstance(value, dict):
        raise TrainingValidationError("Recipe must be a mapping")
    try:
        return TrainingSpec.model_validate(value)
    except ValidationError as error:
        raise TrainingValidationError("Invalid training fields: " + _safe_fields(error)) from None


def parse_submission(
    submission: TrainingSubmission, *, settings: Settings | None = None
) -> ParsedTrainingSubmission:
    config = settings or get_settings()
    encoded = _bounded_source(submission.source_yaml, config)
    spec = parse_spec(submission.source_yaml, settings=config)
    limits = (
        (spec.optim.updates, config.training_max_updates, "optim.updates"),
        (
            spec.optim.max_sequence_length,
            config.training_max_sequence_length,
            "optim.max_sequence_length",
        ),
        (spec.optim.batch_size, config.training_max_batch_size, "optim.batch_size"),
        (
            spec.optim.accumulation_steps,
            config.training_max_accumulation_steps,
            "optim.accumulation_steps",
        ),
        (spec.parameterization.rank, config.training_max_adapter_rank, "parameterization.rank"),
        (len(spec.data.train.datasets), config.training_mixture_max_sources, "data.train.datasets"),
    )
    for value, maximum, field in limits:
        if value > maximum:
            raise TrainingValidationError("Training field exceeds configured bound: " + field)
    if submission.form_spec is not None and submission.form_spec.model_dump(
        mode="json"
    ) != spec.model_dump(mode="json"):
        raise TrainingValidationError("Generated YAML differs from submitted form settings")
    # Only client-declared fields identify a retry. Registry/operator default resolution
    # occurs after the existing command lookup, never inside this client-intent hash.
    intent = spec.model_dump(mode="json", exclude_unset=True)
    intent["schema_version"] = spec.schema_version
    intent_bytes = json.dumps(
        intent, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    return ParsedTrainingSubmission(
        source_yaml=submission.source_yaml,
        source_kind=submission.source_kind,
        source_sha256=hashlib.sha256(encoded).hexdigest(),
        intent_sha256=hashlib.sha256(intent_bytes).hexdigest(),
        spec=spec,
    )


def training_config_digest(spec: TrainingSpec) -> str:
    value = spec.model_dump(mode="json")
    value["output"].pop("adapter_slug")
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def training_recipes() -> TrainingRecipePage:
    """Read only the three shipped versioned assets, never a caller-chosen path."""
    from coire_core.models.training import TrainingRecipe, TrainingRecipePage

    # Source and packaged deployments may supply these same read-only repository assets.
    root = Path(__file__).resolve().parents[5] / "recipes" / "training"
    items = []
    for kind in ("lora", "qlora", "dora"):
        path = root / f"sft-{kind}.yaml"
        try:
            with path.open("rb") as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError()
            source = data.decode("utf-8")
            if not all(
                "${" + binding + "}" in source
                for binding in ("model_id", "variant_id", "dataset_id", "adapter_slug")
            ):
                raise ValueError()
            # Validate template structure using inert synthetic IDs, not runnable registry IDs.
            example = source
            for binding in ("model_id", "variant_id", "dataset_id"):
                example = example.replace(
                    "${" + binding + "}", "00000000-0000-4000-8000-000000000001"
                )
            parsed = parse_spec(example.replace("${adapter_slug}", "template-validation"))
            if parsed.parameterization.kind != kind:
                raise ValueError()
        except (OSError, UnicodeError, ValueError) as error:
            from coire_core.errors import TrainingUnavailable

            raise TrainingUnavailable("Versioned training recipe asset is unavailable") from error
        items.append(
            TrainingRecipe.model_validate(
                {
                    "id": f"sft-{kind}",
                    "version": 1,
                    "parameterization": kind,
                    "template_yaml": source,
                    "required_bindings": ["model_id", "variant_id", "dataset_id", "adapter_slug"],
                }
            )
        )
    return TrainingRecipePage(items=items)


async def resolve_submission(
    session: AsyncSession, submission: TrainingSubmission, *, settings: Settings | None = None
) -> TrainingValidation:
    """Resolve only registry inputs and recorded, strict, current measured evidence."""
    from sqlalchemy import select

    from coire_api.db import (
        ModelRow,
        ModelVariantRow,
        NodeRow,
        TrainingCommandRow,
        TrainingDatasetAnalysisRow,
        TrainingDatasetRevisionRow,
        TrainingMeasurementRow,
        TrainingProfileRow,
        VariantCopyRow,
    )
    from coire_api.training.mixtures import compile_mixture
    from coire_api.training.service import payload_digest, recheck_training_inputs
    from coire_core.errors import TrainingConflict, TrainingNotFound
    from coire_core.models.acquisition import VariantState
    from coire_core.models.datasets import DatasetAnalysis, DatasetAnalysisBinding, SplitManifest
    from coire_core.models.registry import ModelState
    from coire_core.models.training import (
        ResolvedDatasetInput,
        ResolvedTrainingSpec,
        TrainingMeasurementResult,
        TrainingValidation,
    )

    parsed = parse_submission(submission, settings=settings)
    spec = parsed.spec
    model = await session.get(
        ModelRow, spec.model.model_id, populate_existing=True, with_for_update=True
    )
    variant = await session.get(
        ModelVariantRow, spec.model.variant_id, populate_existing=True, with_for_update=True
    )
    if model is None or variant is None or variant.model_id != model.id:
        raise TrainingNotFound()
    if (
        model.state is not ModelState.READY
        or variant.state is not VariantState.READY
        or model.source != "studio"
        or model.kind != "language_model"
        or model.backend != "mlx_lm"
        or variant.backend != "mlx_lm"
    ):
        raise TrainingValidationError("Training model must be an acquired ready local text variant")
    copies = list(
        (
            await session.execute(
                select(NodeRow.name, VariantCopyRow.manifest_sha256)
                .join(VariantCopyRow, VariantCopyRow.node_id == NodeRow.id)
                .where(VariantCopyRow.variant_id == variant.id, VariantCopyRow.verified.is_(True))
            )
        ).all()
    )
    digests = {name: digest for name, digest in copies if name in {"coire-edge-a", "coire-edge-b"}}
    if set(digests) != {"coire-edge-a", "coire-edge-b"} or len(set(digests.values())) != 1:
        raise TrainingConflict("Training base requires matching verified copies on both Studios")
    base = digests["coire-edge-a"]
    if not isinstance(base, str) or re.fullmatch(r"[a-f0-9]{64}", base) is None:
        raise TrainingConflict("Training base has no immutable manifest identity")
    inputs = []
    results = []
    splits = {}
    required = {item.dataset_id for item in spec.data.train.datasets} | set(
        spec.data.validation.dataset_ids
    )
    for dataset_id in sorted(required, key=str):
        source = await session.get(
            TrainingDatasetRevisionRow, dataset_id, populate_existing=True, with_for_update=True
        )
        if source is None:
            raise TrainingNotFound()
        if (
            source.state in {"retired", "purged", "failed", "analysis_failed"}
            or source.purged_at is not None
        ):
            raise TrainingConflict("Training dataset is unavailable")
        if source.state != "ready":
            return TrainingValidation(
                spec=spec, intent_sha256=parsed.intent_sha256, reasons=["analysis_pending"]
            )
        expected_loss = "all_tokens" if source.format == "text" else "final_assistant"
        if spec.data.loss_policy != expected_loss:
            raise TrainingValidationError(
                "data.loss_policy must match the registered dataset supervision mode"
            )
        split = SplitManifest.model_validate(source.split_manifest)
        if (
            payload_digest(split) != source.split_sha256
            or split.source_sha256 != source.source_sha256
        ):
            raise TrainingConflict("Dataset split identity changed")
        splits[dataset_id] = split
        expected = DatasetAnalysisBinding.model_validate(
            {
                "dataset_id": dataset_id,
                "model_id": model.id,
                "variant_id": variant.id,
                "base_manifest_sha256": base,
                "source_sha256": source.source_sha256,
                "split_sha256": source.split_sha256,
                "format": source.format,
                "model_slug": variant.slug,
                "template_override": model.chat_template,
            }
        )
        # Binding digest is generated by the existing dataset service, not by source contents.
        identity = payload_digest(expected)
        analyses = list(
            (
                await session.scalars(
                    select(TrainingDatasetAnalysisRow)
                    .where(
                        TrainingDatasetAnalysisRow.dataset_id == dataset_id,
                        TrainingDatasetAnalysisRow.model_id == model.id,
                        TrainingDatasetAnalysisRow.variant_id == variant.id,
                        TrainingDatasetAnalysisRow.identity_sha256 == identity,
                        TrainingDatasetAnalysisRow.state == "succeeded",
                    )
                    .order_by(
                        TrainingDatasetAnalysisRow.created_at.desc(),
                        TrainingDatasetAnalysisRow.id.desc(),
                    )
                )
            ).all()
        )
        chosen = None
        for analysis in analyses:
            command = await session.get(TrainingCommandRow, analysis.command_id)
            if command is None:
                continue
            frozen = DatasetAnalysisBinding.model_validate(command.payload.get("analysis"))
            if frozen != expected:
                continue
            candidate = DatasetAnalysis.model_validate(analysis.result)
            if (
                candidate.id != analysis.id
                or candidate.dataset_id != dataset_id
                or candidate.state != "succeeded"
            ):
                raise TrainingConflict("Dataset analysis result identity changed")
            chosen = candidate
            break
        if chosen is None:
            return TrainingValidation(
                spec=spec, intent_sha256=parsed.intent_sha256, reasons=["analysis_pending"]
            )
        results.append(chosen)
        inputs.append(
            ResolvedDatasetInput.model_validate(
                {
                    "dataset_id": dataset_id,
                    "analysis_id": chosen.id,
                    "source_sha256": source.source_sha256,
                    "split_sha256": source.split_sha256,
                    "analysis_sha256": payload_digest(chosen),
                }
            )
        )
    compile_mixture(
        spec.data.train,
        splits,
        validation_manifests=[splits[identity] for identity in spec.data.validation.dataset_ids],
    )
    if (
        len(
            {(item.tokenizer_sha256, item.template_sha256, item.runtime_sha256) for item in results}
        )
        != 1
    ):
        return TrainingValidation(
            spec=spec, intent_sha256=parsed.intent_sha256, reasons=["runtime_mismatch"]
        )
    if any(
        item.tokens is None or item.tokens.maximum > spec.optim.max_sequence_length
        for item in results
    ):
        raise TrainingValidationError(
            "Dataset contains sequences beyond the declared training bound"
        )
    identity_result = results[0]
    now = datetime.now(UTC)
    config_sha = training_config_digest(spec)
    profiles = list(
        (
            await session.scalars(
                select(TrainingProfileRow)
                .where(
                    TrainingProfileRow.invalidated_reason.is_(None),
                )
                .order_by(TrainingProfileRow.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    reason: TrainingReason = "profile_missing"
    for profile in profiles:
        measurement = await session.get(TrainingMeasurementRow, profile.measurement_id)
        if measurement is None or measurement.state != "succeeded" or measurement.report is None:
            continue
        report = TrainingMeasurementResult.model_validate(measurement.report)
        evidence = getattr(report, "memory_evidence", None)
        if evidence is None or report.state != "succeeded" or report.id != measurement.id:
            continue
        if (
            training_config_digest(report.request.spec) != config_sha
            or evidence.training_config_sha256 != config_sha
            or evidence.base_manifest_sha256 != base
            or evidence.tokenizer_sha256 != identity_result.tokenizer_sha256
            or evidence.template_sha256 != identity_result.template_sha256
            or evidence.runtime_sha256 != identity_result.runtime_sha256
        ):
            continue
        if (
            spec.placement.mode == "data_parallel"
            and set(report.request.nodes) != {"coire-edge-a", "coire-edge-b"}
        ) or (spec.placement.mode == "single" and len(report.request.nodes) != 1):
            continue
        if evidence.valid_until <= now or profile.valid_until <= now or evidence.measured_at > now:
            reason = "profile_expired"
            continue
        if (
            evidence.measurement_id != report.id
            or evidence.completed_updates < 1
            or report.completed_updates < evidence.completed_updates
            or evidence.valid_until
            > evidence.measured_at
            + timedelta(seconds=(settings or get_settings()).training_profile_ttl_s)
            or any(
                not item.thermal_ok
                or item.swap_growth_bytes != 0
                or item.peak_footprint_bytes > evidence.resource_envelope.memory_bytes
                for item in evidence.nodes
            )
        ):
            continue
        nodes = list(
            (
                await session.scalars(select(NodeRow).where(NodeRow.name.in_(report.request.nodes)))
            ).all()
        )
        from coire_scheduler.training_guard import (
            hardware_digest,
            measurement_report_digest,
            memory_evidence_digest,
        )

        if (
            len(nodes) != len(report.request.nodes)
            or set(report.request.nodes) != {n.node for n in evidence.nodes}
            or any(
                next((hardware_digest(node) for node in nodes if node.name == item.node), None)
                != item.hardware_sha256
                for item in evidence.nodes
            )
            or evidence.resource_envelope.evidence_sha256 != memory_evidence_digest(evidence)
            or report.report_sha256 != profile.report_sha256
            or report.report_sha256 != measurement.report_sha256
            or report.report_sha256 != measurement_report_digest(report)
        ):
            continue
        if (
            spec.placement.preferred_node is not None
            and spec.placement.preferred_node not in report.request.nodes
        ):
            continue
        resolved = ResolvedTrainingSpec.model_validate(
            {
                "spec": spec,
                "base_manifest_sha256": base,
                "datasets": inputs,
                "tokenizer_sha256": identity_result.tokenizer_sha256,
                "template_sha256": identity_result.template_sha256,
                "runtime_sha256": evidence.runtime_sha256,
                "enable_thinking": False,
                "worker_version": evidence.worker_version,
                "resource_envelope": evidence.resource_envelope,
            }
        )
        await recheck_training_inputs(session, resolved)
        return TrainingValidation(
            spec=spec, intent_sha256=parsed.intent_sha256, resolved=resolved, ready_to_run=True
        )
    return TrainingValidation(spec=spec, intent_sha256=parsed.intent_sha256, reasons=[reason])
