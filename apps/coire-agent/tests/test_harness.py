import uuid

import pytest
from pydantic import BaseModel

from coire_agent.harness import Harness, UnverifiedWriteError
from coire_core.models.harness import (
    HarnessMessage,
    HarnessRunRequest,
    ProfileName,
    TaskClass,
)
from coire_core.models.registry import CapabilityProfile, StructuredOutput, ToolCalling


class Output(BaseModel):
    answer: str


class Transport:
    calls = 0

    async def complete(self, messages: list[HarnessMessage], request: HarnessRunRequest) -> object:
        self.calls += 1
        return '<think>reasoning</think>{"answer":"ok"}'


def request(*, task_class: TaskClass) -> HarnessRunRequest:
    return HarnessRunRequest(
        profile=ProfileName.CODING,
        variant_id=uuid.uuid4(),
        task_class=task_class,
        task="task",
        capability_profile=CapabilityProfile(
            tool_calling=ToolCalling.NATIVE,
            structured_output=StructuredOutput.JSON_MODE,
        ),
        context_window=4096,
    )


async def test_unverified_write_is_refused_before_model_call() -> None:
    transport = Transport()
    with pytest.raises(UnverifiedWriteError, match="not harness-verified"):
        await Harness(transport).run_structured(request(task_class=TaskClass.WRITE), Output)
    assert transport.calls == 0


async def test_unverified_read_proceeds_and_reasoning_is_separate() -> None:
    result = await Harness(Transport()).run_structured(request(task_class=TaskClass.READ), Output)
    assert result.output == {"answer": "ok"}
    assert result.reasoning == "reasoning"
