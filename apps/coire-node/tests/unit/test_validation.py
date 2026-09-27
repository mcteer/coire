from __future__ import annotations

import math

import pytest

from coire_core.models.acquisition import ValidationOutcome
from coire_node.validation import (
    compare_perplexity,
    output_is_nondegenerate,
    perplexity,
    smoke_argv,
    validate_tool_call_shape,
)


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
