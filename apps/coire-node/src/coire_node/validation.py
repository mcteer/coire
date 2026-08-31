"""Deterministic validation primitives for converted MLX variants."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any

from coire_core.models.acquisition import ValidationOutcome

SMOKE_PROMPTS = (
    "Reply with exactly one short sentence describing rain.",
    "What is two plus three? Reply with the number and one word.",
)


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
