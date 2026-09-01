"""Deterministic validation primitives for converted MLX variants."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol, cast

from coire_core.models.acquisition import ValidationOutcome

SMOKE_PROMPTS = (
    "Reply with exactly one short sentence describing rain.",
    "What is two plus three? Reply with the number and one word.",
)
HELD_OUT_TEXT = (
    "A durable workflow records completed work so a restart can continue without repeating it. "
    "Checksums establish that two stored copies contain the same bytes."
)


class _ChatTemplateTokenizer(Protocol):
    def apply_chat_template(self, conversation: Any, *, tools: Any, tokenize: bool) -> str: ...


def smoke_argv(model_path: Path, prompt: str, *, python: str = sys.executable) -> list[str]:
    return [
        python,
        "-m",
        "mlx_lm",
        "generate",
        "--model",
        str(model_path),
        "--prompt",
        prompt,
        "--max-tokens",
        "32",
        "--temp",
        "0",
        "--seed",
        "42",
        "--verbose",
        "False",
    ]


def output_is_nondegenerate(output: str) -> bool:
    tokens = output.strip().split()
    if not tokens:
        return False
    if len(tokens) >= 6 and len(set(tokens)) <= 2:
        return False
    longest_run = 1
    run = 1
    for previous, current in pairwise(tokens):
        run = run + 1 if current == previous else 1
        longest_run = max(longest_run, run)
    return longest_run < 5


def run_smoke(model_path: Path) -> tuple[ValidationOutcome, str | None]:
    if os.environ.get("COIRE_TEST_FAKE_VALIDATION") == "1":
        return ValidationOutcome.PASS, None
    for index, prompt in enumerate(SMOKE_PROMPTS):
        result = subprocess.run(
            smoke_argv(model_path, prompt),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode != 0:
            return ValidationOutcome.FAIL, f"prompt {index} generation failed"
        if not output_is_nondegenerate(result.stdout.decode(errors="replace")):
            return ValidationOutcome.FAIL, f"prompt {index} produced degenerate output"
    return ValidationOutcome.PASS, None


def perplexity(mean_negative_log_likelihood: float) -> float:
    if not math.isfinite(mean_negative_log_likelihood) or mean_negative_log_likelihood < 0:
        raise ValueError("mean negative log likelihood must be finite and non-negative")
    return math.exp(mean_negative_log_likelihood)


def compare_perplexity(
    value: float, reference: float | None, tolerance: float
) -> ValidationOutcome:
    if reference is None:
        return ValidationOutcome.NOT_COMPARABLE
    if reference < 0 or tolerance < 0:
        raise ValueError("reference and tolerance must be non-negative")
    return (
        ValidationOutcome.PASS if value <= reference * (1 + tolerance) else ValidationOutcome.FAIL
    )


def validate_tool_call_shape(rendered: str) -> ValidationOutcome:
    """Validate the canonical tool call's JSON shape after template rendering."""
    try:
        value: Any = json.loads(rendered)
    except json.JSONDecodeError:
        return ValidationOutcome.FAIL
    if not isinstance(value, dict):
        return ValidationOutcome.FAIL
    calls = value.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return ValidationOutcome.FAIL
    function = calls[0].get("function")
    if not isinstance(function, dict) or function.get("name") != "coire_validation_echo":
        return ValidationOutcome.FAIL
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return ValidationOutcome.FAIL
    return (
        ValidationOutcome.PASS
        if isinstance(arguments, dict) and arguments == {"value": "ok"}
        else ValidationOutcome.FAIL
    )


def run_template_check(model_path: Path) -> tuple[ValidationOutcome, str | None]:
    if os.environ.get("COIRE_TEST_FAKE_VALIDATION") == "1":
        return ValidationOutcome.PASS, None
    try:
        from mlx_lm.tokenizer_utils import load as load_tokenizer

        tokenizer = cast(_ChatTemplateTokenizer, load_tokenizer(model_path))
        rendered = tokenizer.apply_chat_template(
            [
                {"role": "user", "content": "Echo ok using the tool."},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "coire_validation_echo",
                                "arguments": {"value": "ok"},
                            },
                        }
                    ],
                },
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "coire_validation_echo",
                        "description": "Echo a validation value.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            tokenize=False,
        )
        text = str(rendered)
        if "coire_validation_echo" not in text or "ok" not in text:
            return ValidationOutcome.FAIL, "rendered tool call omitted its name or arguments"
        return ValidationOutcome.PASS, None
    except Exception as exc:
        return ValidationOutcome.FAIL, f"template rendering failed: {type(exc).__name__}"


def measure_perplexity(model_path: Path, text: str = HELD_OUT_TEXT) -> float:
    if os.environ.get("COIRE_TEST_FAKE_VALIDATION") == "1":
        return 10.0
    import mlx.core as mx
    import mlx.nn as nn
    from mlx_lm import load

    loaded = cast(Any, load(str(model_path), lazy=True))
    model, tokenizer = loaded[0], loaded[1]
    token_ids = tokenizer.encode(text)
    if len(token_ids) < 2:
        raise ValueError("held-out fixture produced fewer than two tokens")
    inputs = mx.array(token_ids[:-1])[None, :]
    targets = mx.array(token_ids[1:])[None, :]
    logits = model(inputs)
    loss = nn.losses.cross_entropy(logits, targets, reduction="mean")
    mx.eval(loss)
    return perplexity(float(cast(Any, loss.item())))
