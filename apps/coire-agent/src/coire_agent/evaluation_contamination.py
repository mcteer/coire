"""Studio-only exact input projection; targets and unconsumed samples are excluded."""

import hashlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from opentelemetry import metrics, trace

from coire_core.models.conversation import TextPart
from coire_core.models.datasets import TrainingExample
from coire_core.models.evaluation import EvaluationCase, EvaluationContamination, EvaluationWorkload


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def input_digest(example: TrainingExample) -> str | None:
    if example.content_mode == "text" or example.loss_policy != "final_assistant":
        return None
    return _digest(
        {
            "tools": [tool.model_dump(mode="json") for tool in example.conversation.tools],
            "messages": [
                {
                    "role": message.role,
                    "parts": [part.model_dump(mode="json") for part in message.parts],
                    "tool_calls": [call.model_dump(mode="json") for call in message.tool_calls],
                    "tool_call_id": message.tool_call_id,
                }
                for message in example.conversation.messages[:-1]
            ],
        }
    )


def case_input_digest(prompt: str, *, system: str | None = None) -> str:
    messages = []
    for role, text in [("system", system), ("user", prompt)]:
        if text is not None:
            messages.append(
                {
                    "role": role,
                    "parts": [TextPart(text=text).model_dump(mode="json")],
                    "tool_calls": [],
                    "tool_call_id": None,
                }
            )
    return _digest({"tools": [], "messages": messages})


def scan_inputs(
    fixtures: Sequence[EvaluationCase],
    consumed: Iterable[TrainingExample] | None,
    *,
    data_sha256: list[str],
) -> EvaluationContamination:
    if consumed is None:
        return EvaluationContamination(
            status="unavailable", reason="input_missing", data_sha256=data_sha256
        )
    expected: dict[str, list[str]] = {}
    for case in fixtures:
        expected.setdefault(case_input_digest(case.prompt), []).append(case.id)
    hits: set[str] = set()
    selected = 0
    unsupported = False
    for example in consumed:
        selected += 1
        digest = input_digest(example)
        if digest is None:
            unsupported = True
        elif digest in expected:
            hits.update(expected[digest])
    if unsupported or not selected:
        return EvaluationContamination(
            status="unavailable",
            reason="unsupported_format" if unsupported else "input_missing",
            data_sha256=data_sha256,
            selected_rows=selected,
        )
    ordered = [case.id for case in fixtures if case.id in hits]
    return EvaluationContamination(
        status="overlap" if hits else "clean",
        data_sha256=data_sha256,
        selected_rows=selected,
        checked_cases=len(fixtures),
        hit_count=len(ordered),
        case_ids=ordered,
    )


def _scan_training_inputs(workload: EvaluationWorkload, root: Path) -> EvaluationContamination:
    """Replay checkpoint cursors against authenticated immutable source/split bytes."""
    from coire_agent.evaluation_inputs import read_input
    from coire_core.evaluation_suites import cases
    from coire_core.models.datasets import SplitManifest
    from coire_core.models.training_node import CheckpointWorkerState, MixtureSamplerState
    from coire_core.training_data import compile_mixture, normalize_row, split_digest
    from coire_core.training_sampling import consumed_mixture_rows, consumed_single_rows

    binding = workload.training
    if binding is None:
        return EvaluationContamination(status="not_applicable", reason="no_training_context")
    digests = [source.source_sha256 for source in binding.sources]
    try:
        state = CheckpointWorkerState.model_validate_json(
            read_input(workload, root, "training-state.json")
        )
        if (
            state.job_id,
            state.attempt_id,
            state.fence,
            state.completed_update,
            state.resolved_spec_sha256,
            state.runtime_sha256,
            state.optimizer.batch_size,
            state.optimizer.accumulation_steps,
        ) != (
            binding.job_id,
            binding.training_attempt_id,
            binding.training_fence,
            binding.completed_update,
            binding.resolved_spec_sha256,
            binding.runtime_sha256,
            binding.batch_size,
            binding.accumulation_steps,
        ):
            raise ValueError("training checkpoint input lineage differs")
        manifests = {}
        for source in binding.sources:
            split = SplitManifest.model_validate_json(
                read_input(workload, root, f"split-{source.dataset_id}.json")
            )
            if (
                split.dataset_id != source.dataset_id
                or split.source_sha256 != source.source_sha256
                or split_digest(split) != source.split_sha256
            ):
                raise ValueError("training source split differs")
            manifests[source.dataset_id] = split
        mixture = compile_mixture(binding.mixture, manifests)
        samples = binding.completed_update * binding.batch_size * binding.accumulation_steps
        if isinstance(state.sampler, MixtureSamplerState):
            if state.sampler.epoch * mixture.epoch_samples + state.sampler.cursor != samples:
                raise ValueError("training sampler cursor differs from completed update")
            selected = consumed_mixture_rows(
                identity_sha256=state.sampler.identity_sha256,
                epoch=state.sampler.epoch,
                cursor=state.sampler.cursor,
                sources=[
                    (source.dataset_id, source.rows, source.quota) for source in mixture.sources
                ],
                strategy=mixture.strategy,
                replacement=mixture.replacement,
            )
        else:
            if (
                len(mixture.sources) != 1
                or mixture.replacement
                or state.sampler.epoch * state.sampler.row_count + state.sampler.cursor != samples
            ):
                raise ValueError("legacy sampler cursor differs from consumed inputs")
            compiled_source = mixture.sources[0]
            legacy_digest = hashlib.sha256(
                (
                    compiled_source.split_sha256 + ":train:" + str(list(compiled_source.rows))
                ).encode()
            ).hexdigest()
            if state.sampler.dataset_sha256 != legacy_digest:
                raise ValueError("legacy sampler source differs")
            selected = {
                (compiled_source.dataset_id, row)
                for row in consumed_single_rows(
                    state.sampler, seed=binding.seed, rows=compiled_source.rows
                )
            }

        def examples() -> Iterable[TrainingExample]:
            for source in binding.sources:
                raw = read_input(workload, root, f"source-{source.dataset_id}.jsonl")
                lines = raw.splitlines()
                split = manifests[source.dataset_id]
                if len(lines) != len(split.row_content_sha256):
                    raise ValueError("training source row count differs")
                for row, line in enumerate(lines, 1):
                    if len(line) > 1024**2:
                        raise ValueError("training source row exceeds bound")
                    if (source.dataset_id, row) in selected:
                        example = normalize_row(
                            json.loads(line),
                            format=source.format,
                            dataset_id=source.dataset_id,
                            source_row=row,
                        )
                        if example.content_sha256() != split.row_content_sha256[row - 1]:
                            raise ValueError("training sample differs from frozen split")
                        yield example

        return scan_inputs(
            cases(workload.suite.template.template_id), examples(), data_sha256=digests
        )
    except (OSError, ValueError):
        return EvaluationContamination(
            status="unavailable", reason="input_missing", data_sha256=digests
        )


tracer = trace.get_tracer("coire.agent.evaluation")
scans = metrics.get_meter("coire.agent.evaluation").create_counter(
    "coire_evaluation_input_scans_total"
)


def scan_training_inputs(workload: EvaluationWorkload, root: Path) -> EvaluationContamination:
    with tracer.start_as_current_span(
        "coire.agent.evaluation.contamination",
        attributes={"run_id": str(workload.run_id), "evaluation_id": workload.evaluation_id},
        record_exception=False,
        set_status_on_exception=False,
    ):
        result = _scan_training_inputs(workload, root)
        scans.add(1, {"status": result.status})
        return result
