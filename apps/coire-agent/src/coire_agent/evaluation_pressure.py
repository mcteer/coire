"""Controlled repeated suite pressure, stopped only by a bound node-owned marker."""

import asyncio
import os
import stat
from pathlib import Path

from coire_agent.evaluation import Generate, execute_phase
from coire_core.models.evaluation import (
    EvaluationOutput,
    EvaluationPressureStop,
    EvaluationRuntime,
    EvaluationWorkerResult,
    EvaluationWorkload,
    canonical_digest,
)


def stopped(workload: EvaluationWorkload, path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ValueError("pressure stop marker is unsafe")
        stop = EvaluationPressureStop.model_validate_json(source.read(4097))
    if (
        workload.pressure is None
        or stop.run_id != workload.run_id
        or stop.measurement_id != workload.pressure.measurement_id
        or stop.request_sha256 != canonical_digest(workload)
    ):
        raise ValueError("pressure stop marker belongs to another workload")
    return True


async def execute_pressure(
    workload: EvaluationWorkload,
    generate: Generate,
    *,
    runtime: EvaluationRuntime,
    stop_path: Path,
    prior_outputs: list[EvaluationOutput] | None = None,
    input_root: Path | None = None,
) -> EvaluationWorkerResult:
    if workload.pressure is None:
        raise ValueError("repeated execution requires a measurement binding")
    latest: EvaluationWorkerResult | None = None
    for _ in range(workload.pressure.max_iterations):
        if await asyncio.to_thread(stopped, workload, stop_path):
            if latest is None:
                raise ValueError("pressure stopped before any complete execution")
            return latest
        latest = await execute_phase(
            workload, generate, runtime=runtime, prior_outputs=prior_outputs, input_root=input_root
        )
        if latest.outcome != "succeeded":
            return latest
    assert latest is not None
    return latest
