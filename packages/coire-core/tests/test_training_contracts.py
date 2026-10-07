"""Boundary and compatibility tests for immutable SFT inputs and exact targets."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from coire_core.models.adapters import InferenceTarget, ModelSelector
from coire_core.models.conversation import Conversation, ConversationMessage, TextPart, ToolCall
from coire_core.models.datasets import DatasetMixture, SplitManifest, TrainingExample
from coire_core.models.harness import HarnessEvaluationRequest, HarnessEvaluationTarget
from coire_core.models.runs import RunContainerCreate, RunTokenScope
from coire_core.models.training import (
    CheckpointDetail,
    TrainingMeasurementRequest,
    TrainingMeasurementResult,
    TrainingSpec,
    TrainingSubmission,
)
from coire_core.models.training_node import (
    DatasetInputGrant,
    TrainingArtifactManifest,
    TrainingCommand,
)

MODEL = "10000000-0000-4000-8000-000000000001"
VARIANT = "10000000-0000-4000-8000-000000000002"
DATASET = "10000000-0000-4000-8000-000000000003"
DIGEST = "a" * 64
JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.mark.parametrize("contract", [HarnessEvaluationTarget, RunContainerCreate])
@pytest.mark.parametrize(
    "mutation",
    ["parent", "variant", "selector_parent", "missing_selector", "base_selector", "missing_target"],
)
def test_exact_transport_contract_rejects_inconsistent_subject(
    contract: type[HarnessEvaluationTarget] | type[RunContainerCreate], mutation: str
) -> None:
    target = InferenceTarget(
        model_id=uuid.UUID(MODEL),
        variant_id=uuid.UUID(VARIANT),
        adapter_id=uuid.uuid4(),
        base_manifest_sha256=DIGEST,
        adapter_manifest_sha256="b" * 64,
    )
    payload: dict[str, object] = {
        "model_id": MODEL,
        "variant_id": VARIANT,
        "target": target.model_dump(mode="json"),
        "public_selector": f"{MODEL}@trained",
    }
    if contract is HarnessEvaluationTarget:
        payload["capability_profile"] = {}
    else:
        payload.update(
            run_id=str(uuid.uuid4()),
            profile="general",
            image=f"ghcr.io/coire/agent@sha256:{DIGEST}",
            argv=["-m", "coire_agent"],
            workspace_ref="safe",
            run_token="r" * 48,
            gateway_url="http://coire-core.lab/v1",
            limits={},
        )
    contract.model_validate(payload)
    changes: dict[str, dict[str, object]] = {
        "parent": {"model_id": str(uuid.uuid4())},
        "variant": {"variant_id": str(uuid.uuid4())},
        "selector_parent": {"public_selector": f"{uuid.uuid4()}@trained"},
        "missing_selector": {"public_selector": None},
        "base_selector": {"public_selector": MODEL},
        "missing_target": {"target": None},
    }
    with pytest.raises(ValidationError):
        contract.model_validate({**payload, **changes[mutation]})


def recipe() -> dict[str, object]:
    return {
        "model": {"model_id": MODEL, "variant_id": VARIANT},
        "data": {
            "train": {
                "datasets": [{"dataset_id": DATASET, "sample_count": 8, "mixture_proportion": 1.0}],
                "epoch_samples": 8,
            },
            "validation": {"dataset_ids": [DATASET]},
        },
        "parameterization": {
            "kind": "lora",
            "target_modules": ["self_attn.q_proj", "self_attn.v_proj"],
        },
        "optim": {"updates": 32, "batch_size": 2, "accumulation_steps": 2},
        "output": {"adapter_slug": "contract-test"},
    }


def conversation() -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        messages=[
            ConversationMessage(
                id=uuid.uuid4(), role="user", parts=[TextPart(text="Sum two and three.")]
            ),
            ConversationMessage(
                id=uuid.uuid4(),
                role="assistant",
                parts=[],
                tool_calls=[ToolCall(id="sum-1", name="add", arguments={"a": 2, "b": 3})],
            ),
            ConversationMessage(
                id=uuid.uuid4(), role="tool", parts=[TextPart(text="5")], tool_call_id="sum-1"
            ),
            ConversationMessage(id=uuid.uuid4(), role="assistant", parts=[TextPart(text="5")]),
        ],
    )


def test_existing_conversation_without_training_fields_roundtrips() -> None:
    value = Conversation(
        id=uuid.uuid4(),
        messages=[ConversationMessage(id=uuid.uuid4(), role="user", parts=[TextPart(text="Hi")])],
    )
    assert Conversation.model_validate_json(value.model_dump_json()) == value


def test_tool_only_assistant_and_resolved_tool_response_are_valid() -> None:
    value = TrainingExample(conversation=conversation(), source_row=1)
    assert value.conversation.messages[1].parts == []


@pytest.mark.parametrize("role", ["user", "system", "tool", "assistant"])
def test_empty_message_without_a_tool_call_is_rejected(role: str) -> None:
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate({"id": str(uuid.uuid4()), "role": role, "parts": []})


@pytest.mark.parametrize("mutation", ["orphan", "duplicate", "unresolved", "wrong_role"])
def test_training_rejects_malformed_tool_relationships(mutation: str) -> None:
    value = conversation().model_dump(mode="json")
    messages = value["messages"]
    if mutation == "orphan":
        messages[2]["tool_call_id"] = "unknown"
    elif mutation == "duplicate":
        messages.insert(3, dict(messages[2]))
    elif mutation == "unresolved":
        messages.pop(2)
    else:
        messages[1]["role"] = "user"
    with pytest.raises(ValidationError):
        TrainingExample.model_validate({"conversation": value, "source_row": 1})


def test_training_refuses_images_but_chat_still_accepts_them() -> None:
    value = {
        "id": str(uuid.uuid4()),
        "messages": [
            {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "parts": [
                    {
                        "type": "image",
                        "asset_id": str(uuid.uuid4()),
                        "media_type": "image/png",
                        "width": 32,
                        "height": 32,
                    }
                ],
            }
        ],
    }
    assert Conversation.model_validate(value)
    with pytest.raises(ValidationError, match="image"):
        TrainingExample.model_validate({"conversation": value, "source_row": 1})


def test_metadata_is_bounded_and_cannot_claim_authority() -> None:
    value = conversation().model_dump(mode="json")
    value["metadata"] = {"verified": True}
    with pytest.raises(ValidationError, match="reserved"):
        Conversation.model_validate(value)
    value["metadata"] = {"note": "x" * 65537}
    with pytest.raises(ValidationError, match="bound"):
        Conversation.model_validate(value)


def test_content_identity_excludes_ids_and_metadata() -> None:
    a = TrainingExample(conversation=conversation(), source_row=1)
    b = TrainingExample(conversation=conversation(), source_row=2)
    assert a.content_sha256() == b.content_sha256()
    b.conversation.messages[-1].parts = [TextPart(text="6")]
    assert a.content_sha256() != b.content_sha256()


def test_recipe_defaults_and_seed_zero_are_preserved() -> None:
    value = TrainingSpec.model_validate(recipe())
    assert value.seed == 0 and value.objective == "sft"
    assert value.eval.at_end and value.optim.name == "adamw"
    assert TrainingSpec.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize("field,value", [("objective", "dpo"), ("unknown", True)])
def test_recipe_rejects_unimplemented_or_unknown_fields(field: str, value: object) -> None:
    data = recipe()
    data[field] = value
    with pytest.raises(ValidationError):
        TrainingSpec.model_validate(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 0.0])
def test_recipe_rejects_nonfinite_or_nonpositive_learning_rate(value: float) -> None:
    data = recipe()
    data["optim"] = {"updates": 32, "learning_rate": value}
    with pytest.raises(ValidationError):
        TrainingSpec.model_validate(data)


def test_recipe_refuses_task_judge_evaluation_fields() -> None:
    data = recipe()
    data["eval"] = {"suites": ["judge"]}
    with pytest.raises(ValidationError):
        TrainingSpec.model_validate(data)


def test_data_parallel_global_batch_must_be_divisible_by_world_size() -> None:
    data = recipe()
    data["placement"] = {"mode": "data_parallel"}
    data["optim"] = {"updates": 32, "batch_size": 3}
    with pytest.raises(ValidationError, match="divisible"):
        TrainingSpec.model_validate(data)


def test_epoch_cannot_silently_drop_a_tail_batch() -> None:
    data = recipe()
    data["optim"] = {"updates": 32, "batch_size": 3}
    with pytest.raises(ValidationError, match="epoch"):
        TrainingSpec.model_validate(data)


def test_mixture_proportions_and_source_identities_are_strict() -> None:
    source = {"dataset_id": DATASET, "sample_count": 8, "mixture_proportion": 0.6}
    with pytest.raises(ValidationError, match="proportions"):
        DatasetMixture.model_validate({"datasets": [source], "epoch_samples": 8})
    with pytest.raises(ValidationError, match="unique"):
        DatasetMixture.model_validate(
            {"datasets": [{**source, "mixture_proportion": 0.5}] * 2, "epoch_samples": 8}
        )


def test_original_yaml_is_not_reformatted_and_is_byte_bounded() -> None:
    source = "# retained comment\nmodel: {}\n"
    value = TrainingSubmission(source_yaml=source)
    assert value.source_yaml == source
    with pytest.raises(ValidationError):
        TrainingSubmission(source_yaml="é" * 32769)
    with pytest.raises(ValidationError):
        TrainingSubmission(source_yaml=source, source_kind="form")


def test_selectors_preserve_uuid_compatibility() -> None:
    adapter: TypeAdapter[ModelSelector] = TypeAdapter(ModelSelector)
    assert adapter.validate_python(MODEL) == uuid.UUID(MODEL)
    assert adapter.validate_python(f"{MODEL}@coder-sft") == f"{MODEL}@coder-sft"


@pytest.mark.parametrize(
    "value",
    [
        "org/model",
        "/opt/models/base",
        f"{MODEL}@../escape",
        f"{MODEL}@one@two",
        f"{MODEL}@",
        f"{MODEL}@CAPS",
    ],
)
def test_selector_cannot_carry_an_engine_path(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ModelSelector).validate_python(value)


def test_target_requires_adapter_manifest_exactly_when_adapter_is_present() -> None:
    base = {"model_id": MODEL, "variant_id": VARIANT, "base_manifest_sha256": DIGEST}
    assert InferenceTarget.model_validate(base).adapter_id is None
    with pytest.raises(ValidationError, match="adapter"):
        InferenceTarget.model_validate({**base, "adapter_id": str(uuid.uuid4())})
    with pytest.raises(ValidationError, match="adapter"):
        InferenceTarget.model_validate({**base, "adapter_manifest_sha256": DIGEST})


def test_legacy_run_scope_has_no_implicit_exact_adapter_grants() -> None:
    scope = RunTokenScope(permitted_model_ids=frozenset({uuid.UUID(MODEL)}), spend_limit_tokens=100)
    assert scope.permitted_targets == ()
    target = InferenceTarget(
        model_id=uuid.UUID(MODEL),
        variant_id=uuid.UUID(VARIANT),
        adapter_id=uuid.uuid4(),
        base_manifest_sha256=DIGEST,
        adapter_manifest_sha256=DIGEST,
    )
    granted = RunTokenScope(
        permitted_model_ids=scope.permitted_model_ids,
        spend_limit_tokens=100,
        permitted_targets=(target,),
    )
    assert granted.permitted_targets == (target,)
    with pytest.raises(ValidationError):
        RunTokenScope(
            permitted_model_ids=frozenset({uuid.uuid4()}),
            spend_limit_tokens=100,
            permitted_targets=(target,),
        )


def test_legacy_evaluator_rejects_adapter_subject_until_exact_route_is_implemented() -> None:
    with pytest.raises(ValidationError):
        HarnessEvaluationRequest.model_validate(
            {"variant_id": VARIANT, "adapter_id": str(uuid.uuid4())}
        )


def test_node_command_refuses_arbitrary_argv_and_invalid_rank() -> None:
    data = {
        "command_id": str(uuid.uuid4()),
        "job_id": JOB,
        "attempt_id": JOB,
        "fence": 1,
        "request_sha256": DIGEST,
        "node": "coire-edge-a",
        "rank": 0,
        "world_size": 1,
        "lease_expires_at": datetime.now(UTC).isoformat(),
    }
    assert TrainingCommand.model_validate(data)
    with pytest.raises(ValidationError):
        TrainingCommand.model_validate({**data, "argv": ["sh", "-c", "bad"]})
    with pytest.raises(ValidationError, match="rank"):
        TrainingCommand.model_validate({**data, "rank": 1})


def test_artifact_manifest_refuses_traversal_duplicate_files_and_incorrect_total() -> None:
    item = {"id": "adapter", "name": "adapter.safetensors", "bytes": 4, "sha256": DIGEST}
    data = {"artifact_id": str(uuid.uuid4()), "kind": "adapter", "files": [item], "total_bytes": 4}
    assert TrainingArtifactManifest.model_validate(data)
    for invalid in (
        {**data, "files": [{**item, "name": "../escape"}]},
        {**data, "files": [item, item], "total_bytes": 8},
        {**data, "total_bytes": 5},
    ):
        with pytest.raises(ValidationError):
            TrainingArtifactManifest.model_validate(invalid)


def test_split_refuses_exact_duplicate_leakage() -> None:
    data = {
        "dataset_id": DATASET,
        "source_sha256": DIGEST,
        "seed": 0,
        "train_rows": [1],
        "validation_rows": [2],
        "row_content_sha256": [DIGEST, DIGEST],
    }
    with pytest.raises(ValidationError, match="duplicate content"):
        SplitManifest.model_validate(data)
    data["row_content_sha256"] = [DIGEST, "b" * 64]
    assert SplitManifest.model_validate(data)
    with pytest.raises(ValidationError, match="partition"):
        SplitManifest.model_validate({**data, "validation_rows": [1]})


def test_committed_checkpoint_requires_both_physical_replica_identities() -> None:
    data = {
        "id": str(uuid.uuid4()),
        "job_id": JOB,
        "attempt_id": JOB,
        "fence": 1,
        "update": 2,
        "manifest_sha256": DIGEST,
        "total_bytes": 4,
        "state": "committed",
        "created_at": datetime.now(UTC),
        "verified_nodes": ["coire-edge-a"],
    }
    with pytest.raises(ValidationError, match="both verified"):
        CheckpointDetail.model_validate(data)
    data["verified_nodes"] = ["coire-edge-a", "coire-edge-b"]
    assert CheckpointDetail.model_validate(data)


def test_dataset_grant_cannot_expand_execution_scope() -> None:
    data = {
        "grant_id": str(uuid.uuid4()),
        "node": "coire-edge-a",
        "dataset_id": DATASET,
        "source_sha256": DIGEST,
        "max_bytes": 4,
        "expires_at": datetime.now(UTC),
        "secret": "x" * 32,
    }
    with pytest.raises(ValidationError, match="exactly one"):
        DatasetInputGrant.model_validate(data)
    grant = DatasetInputGrant.model_validate({**data, "analysis_id": str(uuid.uuid4())})
    assert "x" * 32 not in repr(grant)
    with pytest.raises(ValidationError, match="exactly one"):
        DatasetInputGrant.model_validate(
            {**data, "analysis_id": str(uuid.uuid4()), "attempt_id": JOB}
        )


def test_measurement_cannot_approve_under_sampled_or_missing_targets() -> None:
    instance = uuid.uuid4()
    request = TrainingMeasurementRequest.model_validate(
        {
            "spec": recipe(),
            "nodes": ["coire-edge-a"],
            "mode": "coexistence",
            "resident_targets": [
                {
                    "instance_id": instance,
                    "target": {
                        "model_id": MODEL,
                        "variant_id": VARIANT,
                        "base_manifest_sha256": DIGEST,
                    },
                }
            ],
            "workload": {
                "sha256": DIGEST,
                "concurrency_per_target": 1,
                "arrival_interval_ms": 1000,
                "max_output_tokens": 16,
            },
        }
    )
    data = {
        "id": uuid.uuid4(),
        "request": request,
        "state": "succeeded",
        "report_sha256": DIGEST,
        "profile_id": uuid.uuid4(),
        "completed_updates": 1,
        "thermal_ok": True,
        "created_at": datetime.now(UTC),
        "targets": [
            {
                "instance_id": instance,
                "baseline_requests": 100,
                "mixed_requests": 99,
                "baseline_p95_seconds": 1.0,
                "mixed_p95_seconds": 1.0,
            }
        ],
    }
    with pytest.raises(ValidationError, match="sampled"):
        TrainingMeasurementResult.model_validate(data)
    with pytest.raises(ValidationError, match="every declared"):
        TrainingMeasurementResult.model_validate({**data, "targets": []})
    data["targets"] = [
        {
            "instance_id": instance,
            "baseline_requests": 100,
            "mixed_requests": 100,
            "baseline_p95_seconds": 1.0,
            "mixed_p95_seconds": 1.0,
        }
    ]
    assert TrainingMeasurementResult.model_validate(data)
    with pytest.raises(ValidationError, match="safe recorded"):
        TrainingMeasurementResult.model_validate({**data, "swap_growth_bytes": 1})
