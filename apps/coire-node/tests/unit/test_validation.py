from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from coire_core.models.acquisition import NodeValidateRequest, ValidationOutcome, ValidationResult
from coire_node.validation import (
    compare_perplexity,
    output_is_nondegenerate,
    perplexity,
    run_template_check,
    smoke_argv,
    validate_tool_call_shape,
)
from coire_node.visual_validation import inspect_local_variant, run_visual_smoke
from coire_node.worker import EXIT_FAILED, EXIT_OK, run_validate


def test_smoke_argv_is_local_deterministic_and_cannot_trust_remote_code(tmp_path) -> None:  # type: ignore[no-untyped-def]
    argv = smoke_argv(tmp_path / "model", "hello", python="python")
    assert argv[:4] == ["python", "-m", "mlx_lm", "generate"]
    assert "--seed" in argv and argv[argv.index("--seed") + 1] == "42"
    assert "--trust-remote-code" not in argv


@pytest.mark.parametrize("output", ["", "x x x x x x", "word " * 20])
def test_degenerate_smoke_output_fails(output: str) -> None:
    assert not output_is_nondegenerate(output)


def test_normal_smoke_output_passes() -> None:
    assert output_is_nondegenerate("Rain falls when condensed water droplets become heavy.")


def test_perplexity_and_reference_tolerance() -> None:
    assert perplexity(math.log(10)) == pytest.approx(10)
    assert compare_perplexity(10.5, 10, 0.1) is ValidationOutcome.PASS
    assert compare_perplexity(11.1, 10, 0.1) is ValidationOutcome.FAIL
    assert compare_perplexity(10, None, 0.1) is ValidationOutcome.NOT_COMPARABLE


def test_tool_call_shape_rejects_malformed_and_accepts_canonical() -> None:
    assert validate_tool_call_shape("not json") is ValidationOutcome.FAIL
    rendered = (
        '{"tool_calls":[{"function":{"name":"coire_validation_echo","arguments":{"value":"ok"}}}]}'
    )
    assert validate_tool_call_shape(rendered) is ValidationOutcome.PASS


def test_simple_chat_template_is_valid_without_tool_capability(tmp_path: Path) -> None:
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": "{{ message['content'] }}"})
    )

    class PlainTokenizer:
        def apply_chat_template(
            self, conversation: object, *, tools: object, tokenize: bool
        ) -> str:
            assert tools is None
            assert not tokenize
            assert isinstance(conversation, list)
            return str(conversation[0]["content"])

    tokenizer_utils = ModuleType("mlx_lm.tokenizer_utils")
    tokenizer_utils.load = lambda path: PlainTokenizer()  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"mlx_lm.tokenizer_utils": tokenizer_utils}):
        assert run_template_check(tmp_path) == (ValidationOutcome.NOT_APPLICABLE, None)


def _visual_files(path: Path) -> None:
    path.mkdir()
    (path / "config.json").write_text(
        json.dumps({"architectures": ["Idefics3ForConditionalGeneration"]})
    )
    for name in ("processor_config.json", "preprocessor_config.json", "tokenizer_config.json"):
        (path / name).write_text("{}")
    (path / "tokenizer.json").write_text("{}")
    (path / "model.safetensors").write_bytes(b"weights")


def test_visual_inventory_requires_local_complete_processor_and_weights(tmp_path: Path) -> None:
    model = tmp_path / "model"
    _visual_files(model)
    assert inspect_local_variant(model) is None
    (model / "processor_config.json").unlink()
    assert (
        inspect_local_variant(model) == "local visual processor or tokenizer files are incomplete"
    )
    (model / "processor_config.json").symlink_to(tmp_path / "elsewhere")
    assert inspect_local_variant(model) == "local visual variant contains a symbolic link"


def test_visual_smoke_uses_only_local_loader_and_fails_closed(tmp_path: Path) -> None:
    model = tmp_path / "model"
    _visual_files(model)
    # mlx and mlx-vlm are Darwin node extras. The unit test supplies their loader
    # interface so Linux CI can prove the smoke stays local and fails closed.
    mlx_pkg = ModuleType("mlx")
    mlx_core = ModuleType("mlx.core")
    mlx_core.metal = SimpleNamespace(  # type: ignore[attr-defined]
        reset_peak_memory=lambda: None,
        get_peak_memory=lambda: 1,
        get_cache_memory=lambda: 1,
    )
    mlx_pkg.core = mlx_core  # type: ignore[attr-defined]
    mlx_vlm = ModuleType("mlx_vlm")
    load = MagicMock(return_value=(object(), object()))
    generate = MagicMock(return_value=SimpleNamespace(text="The square is red."))
    mlx_vlm.load = load  # type: ignore[attr-defined]
    mlx_vlm.generate = generate  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"mlx": mlx_pkg, "mlx.core": mlx_core, "mlx_vlm": mlx_vlm}):
        outcome, failure, capability = run_visual_smoke(model)
    assert outcome is ValidationOutcome.PASS
    assert failure is None
    assert capability is not None and capability.verified
    load.assert_called_once_with(str(model), trust_remote_code=False, strict=True)
    assert "<image>" in generate.call_args.args[2]
    assert generate.call_args.kwargs["image"].endswith("fixture.png")

    load.side_effect = RuntimeError("sensitive path")
    with patch.dict(sys.modules, {"mlx": mlx_pkg, "mlx.core": mlx_core, "mlx_vlm": mlx_vlm}):
        outcome, failure, capability = run_visual_smoke(model)
    assert outcome is ValidationOutcome.FAIL
    assert failure == "visual load failed: RuntimeError"
    assert capability is None


def test_node_validate_contract_rejects_unknown_backend_and_fields() -> None:
    import uuid

    from pydantic import ValidationError

    command = {"job_id": str(uuid.uuid4()), "slug": "tiny-vision"}
    assert NodeValidateRequest.model_validate(command).backend.value == "mlx_lm"
    assert (
        NodeValidateRequest.model_validate({**command, "backend": "mlx_vlm"}).backend.value
        == "mlx_vlm"
    )
    with pytest.raises(ValidationError):
        NodeValidateRequest.model_validate({**command, "backend": "other"})
    with pytest.raises(ValidationError):
        NodeValidateRequest.model_validate({**command, "trust_remote_code": True})


@pytest.mark.parametrize("passes", [True, False])
def test_visual_worker_records_result_without_text_validation(passes: bool, tmp_path: Path) -> None:
    from coire_core.models.registry import VisualCapability

    job = MagicMock()
    job.params = {"backend": "mlx_vlm", "validator_version": "v1"}
    job.status.slug = "tiny-vision"
    store = MagicMock()
    store.read_manifest.return_value = object()
    store.verify_against.return_value = []
    store.path_for.return_value = tmp_path
    visual = VisualCapability(
        verified=True,
        max_images=1,
        max_image_pixels=256,
        max_encoded_bytes=80,
    )
    outcome = ValidationOutcome.PASS if passes else ValidationOutcome.FAIL
    with (
        patch("coire_node.worker._store", return_value=store),
        patch(
            "coire_node.visual_validation.run_visual_smoke",
            return_value=(outcome, None, visual if passes else None),
        ),
        patch("coire_node.validation.measure_perplexity") as perplexity_measure,
    ):
        exit_code = run_validate(job)
    perplexity_measure.assert_not_called()
    result = job.finish.call_args.kwargs["result"] if passes else job.status.result
    parsed = ValidationResult.model_validate(result)
    assert parsed.backend.value == "mlx_vlm"
    assert parsed.validated is passes
    assert parsed.perplexity_outcome is ValidationOutcome.NOT_COMPARABLE
    assert parsed.visual_input is not None if passes else parsed.visual_input is None
    assert exit_code == (EXIT_OK if passes else EXIT_FAILED)
