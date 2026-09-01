from pydantic import BaseModel, ConfigDict

from coire_agent.outputs import validate_output


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
