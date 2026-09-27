"""Benchmark API projection."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import BenchmarkRunRow, PlacementBenchmarkRow
from coire_core.models import BenchmarkResult, BenchmarkRun


async def project_run(session: AsyncSession, row: BenchmarkRunRow) -> BenchmarkRun:
    results = list(
        (
            await session.execute(
                select(PlacementBenchmarkRow)
                .where(PlacementBenchmarkRow.run_id == row.id)
                .order_by(PlacementBenchmarkRow.run_at)
            )
        )
        .scalars()
        .all()
    )
    return BenchmarkRun(
        id=row.id,
        variant_id=row.variant_id,
        state=row.state,
        prompt_tokens=row.prompt_tokens,
        generation_tokens=row.generation_tokens,
        results=[
            BenchmarkResult(
                id=result.id,
                run_id=result.run_id,
                variant_id=result.variant_id,
                placement=result.placement,
                tokens_per_second=result.tokens_per_second,
                prompt_tokens=result.prompt_tokens,
                generation_tokens=result.generation_tokens,
                gpu_cores=result.gpu_cores,
                os_versions=result.os_versions,
                engine_version=result.engine_version,
                failure=result.failure,
                run_at=result.run_at,
            )
            for result in results
        ],
        failure=row.failure,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )
