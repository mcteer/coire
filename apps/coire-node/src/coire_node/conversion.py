"""Bare mlx-lm conversion with allowlisted argv and atomic publication."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from coire_core.models.acquisition import Precision, QuantizationMode, VariantRecipe

ALLOWED_MIXED_RECIPES = frozenset({"mixed_2_6", "mixed_3_4", "mixed_3_6", "mixed_4_6"})


def build_convert_argv(
    source: Path, target: Path, recipe: VariantRecipe, *, python: str = sys.executable
) -> list[str]:
    argv = [python, "-m", "mlx_lm", "convert", "--hf-path", str(source), "--mlx-path", str(target)]
    if recipe.precision in {Precision.BF16, Precision.FP16}:
        dtype = "bfloat16" if recipe.precision is Precision.BF16 else "float16"
        argv.extend(["--dtype", dtype])
        return argv
    argv.append("--quantize")
    bits = recipe.bits
    if bits is None and recipe.precision is not Precision.MIXED:
        bits = int(recipe.precision.value.removesuffix("bit"))
    if bits is not None:
        argv.extend(["--q-bits", str(bits)])
    if recipe.group_size is not None:
        argv.extend(["--q-group-size", str(recipe.group_size)])
    if recipe.mode is not None:
        if recipe.mode not in set(QuantizationMode):
            raise ValueError("unsupported quantization mode")
        argv.extend(["--q-mode", recipe.mode.value])
    if recipe.mixed_recipe is not None:
        if recipe.mixed_recipe not in ALLOWED_MIXED_RECIPES:
            raise ValueError("unsupported mixed recipe")
        argv.extend(["--quant-predicate", recipe.mixed_recipe])
    return argv


def convert_atomic(
    *, source: Path, destination: Path, recipe: VariantRecipe, job_suffix: str
) -> subprocess.CompletedProcess[bytes]:
    partial = destination.with_name(f"{destination.name}.partial-{job_suffix}")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    try:
        result = subprocess.run(
            build_convert_argv(source, partial, recipe),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=None,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"mlx_lm.convert exited {result.returncode}: {result.stdout[-2000:].decode(errors='replace')}"
            )
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination.name}")
        partial.replace(destination)
        return result
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def recipe_from_params(params: dict[str, Any]) -> VariantRecipe:
    return VariantRecipe.model_validate(params)
