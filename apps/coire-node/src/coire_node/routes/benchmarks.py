from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from coire_core.models import BenchmarkCommand, BenchmarkMeasurement
from coire_node.benchmarks import BenchmarkRunner

router = APIRouter(prefix="/node/benchmarks", tags=["benchmarks"])


@router.post("", response_model=BenchmarkMeasurement)
async def run_benchmark(command: BenchmarkCommand, request: Request) -> BenchmarkMeasurement:
    runner = request.app.state.benchmarks
    if not isinstance(runner, BenchmarkRunner):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "benchmarks unavailable")
    try:
        return runner.run(command)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "verified model copy missing") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
