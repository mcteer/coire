from pydantic import BaseModel, ConfigDict

from coire_agent.outputs import validate_output
from coire_core.models.harness import HarnessStrategy


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


async def test_invalid_output_retries_with_validation_feedback() -> None:
    errors: list[str] = []

    async def retry(error: str) -> str:
        errors.append(error)
        return '{"answer":"fixed"}'

    result, retries = await validate_output("bad", Result, retry=retry)
    assert result.answer == "fixed" and retries == 1 and errors


async def test_repair_is_last_resort() -> None:
    async def repair(raw: str, error: str) -> str:
        return '{"answer":"repaired"}'

    result, retries = await validate_output("bad", Result, repair=repair, retry_limit=0)
    assert result.answer == "repaired" and retries == 1


async def test_missing_delimiters_retry_with_feedback() -> None:
    errors: list[str] = []

    async def retry(error: str) -> str:
        errors.append(error)
        return '<output>{"answer":"fixed"}</output>'

    result, retries = await validate_output(
        "no object", Result, retry=retry, strategy=HarnessStrategy.DELIMITED
    )
    assert result.answer == "fixed" and retries == 1
    assert "does not contain a JSON object" in errors[0]


async def test_delimited_strategy_accepts_fenced_json_object() -> None:
    result, retries = await validate_output(
        '```json\n{"answer":"readme"}\n```<|im_end|>',
        Result,
        strategy=HarnessStrategy.DELIMITED,
    )
    assert result.answer == "readme" and retries == 0
