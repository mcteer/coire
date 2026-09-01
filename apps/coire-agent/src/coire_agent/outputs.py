"""Structured output validation with bounded feedback and optional repair."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ValidationError

from coire_core.models.harness import HarnessStrategy

RepairFn = Callable[[str, str], Awaitable[str | None]]


def reduced_schema(output_type: type[BaseModel]) -> dict[str, object]:
    schema = output_type.model_json_schema()
    return {
        key: value
        for key, value in schema.items()
        if key in {"type", "properties", "required", "$defs"}
    }


def normalize_output(raw: str, strategy: HarnessStrategy) -> str:
    text = raw.strip()
    if strategy is HarnessStrategy.DELIMITED:
        start, end = "<output>", "</output>"
        if start not in text or end not in text:
            raise ValueError("delimited output is missing <output> markers")
        return text.split(start, 1)[1].split(end, 1)[0].strip()
    return text


async def validate_output[T: BaseModel](
    raw: str,
    output_type: type[T],
    *,
    retry: Callable[[str], Awaitable[str]] | None = None,
    repair: RepairFn | None = None,
    retry_limit: int = 2,
    strategy: HarnessStrategy = HarnessStrategy.JSON,
) -> tuple[T, int]:
    candidate = normalize_output(raw, strategy)
    for attempt in range(retry_limit + 1):
        try:
            return output_type.model_validate(json.loads(candidate)), attempt
        except (json.JSONDecodeError, ValidationError) as exc:
            error = str(exc)[:1000]
            if attempt < retry_limit and retry is not None:
                candidate = normalize_output(await retry(error), strategy)
                continue
            if repair is not None:
                repaired = await repair(candidate, error)
                if repaired is not None:
                    return output_type.model_validate(json.loads(repaired)), attempt + 1
            raise ValueError(f"structured output validation failed: {error}") from exc
    raise AssertionError("unreachable")
