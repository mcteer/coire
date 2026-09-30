"""Coding visual references survive context preparation and use fixed control files."""

from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image
from pydantic import BaseModel

from coire_agent import gateway_model
from coire_agent.context import prepare_context
from coire_agent.harness import Harness, UnverifiedWriteError
from coire_core.models.conversation import ImagePart
from coire_core.models.harness import HarnessMessage, HarnessRunRequest, ProfileName, TaskClass
from coire_core.models.registry import CapabilityProfile, StructuredOutput, ToolCalling


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (240, 32, 16)).save(output, format="PNG")
    return output.getvalue()


async def test_current_visual_input_survives_summary_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = uuid.uuid4()
    image = _png()
    (tmp_path / f"{asset_id}.png").write_bytes(image)
    monkeypatch.setattr(gateway_model, "CONTROL_IMAGE_ROOT", tmp_path)
    visual = ImagePart(asset_id=asset_id, media_type="image/png", width=16, height=16)
    history = [HarnessMessage(role="assistant", content=str(index)) for index in range(8)]

    async def summarize(_messages: object) -> str:
        return "prior context"

    view, budget = await prepare_context(
        system_prompt="system",
        task="describe",
        history=history,
        token_limit=100,
        reported_prompt_tokens=90,
        summarize=summarize,
        tool_byte_cap=100,
        visual_inputs=[visual],
    )
    assert budget.summarized_messages == 4
    assert view[-1].visual_inputs == [visual]
    first = await gateway_model._message_content(view[-1])
    retry = await gateway_model._message_content(view[-1])
    assert first == retry
    assert isinstance(first, list)
    assert first[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(
        image
    ).decode("ascii")


async def test_visual_control_file_refuses_changed_dimensions_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = uuid.uuid4()
    path = tmp_path / f"{asset_id}.png"
    path.write_bytes(_png())
    monkeypatch.setattr(gateway_model, "CONTROL_IMAGE_ROOT", tmp_path)
    changed = HarnessMessage(
        role="user",
        content="inspect",
        visual_inputs=[ImagePart(asset_id=asset_id, media_type="image/png", width=8, height=8)],
    )
    with pytest.raises(ValueError, match="changed"):
        await gateway_model._message_content(changed)
    path.unlink()
    path.symlink_to(tmp_path / "outside.png")
    with pytest.raises(ValueError, match="unavailable"):
        await gateway_model._message_content(changed)


async def test_visual_input_is_retained_on_validation_retry_and_write_gate() -> None:
    visual = ImagePart(asset_id=uuid.uuid4(), media_type="image/png", width=16, height=16)

    class Output(BaseModel):
        answer: str

    class Transport:
        def __init__(self) -> None:
            self.calls: list[list[HarnessMessage]] = []

        async def complete(
            self, messages: list[HarnessMessage], _request: HarnessRunRequest
        ) -> str:
            self.calls.append(messages)
            return "invalid" if len(self.calls) == 1 else '{"answer":"ok"}'

    request = HarnessRunRequest(
        profile=ProfileName.CODING,
        variant_id=uuid.uuid4(),
        task_class=TaskClass.READ,
        task="inspect",
        visual_inputs=[visual],
        capability_profile=CapabilityProfile(
            tool_calling=ToolCalling.NATIVE, structured_output=StructuredOutput.JSON_MODE
        ),
        context_window=4096,
    )
    transport = Transport()
    result = await Harness(transport).run_structured(request, Output)
    assert result.output == {"answer": "ok"}
    assert len(transport.calls) == 2
    assert transport.calls[0][-1].visual_inputs == [visual]
    assert transport.calls[1][-2].visual_inputs == [visual]

    blocked = request.model_copy(update={"task_class": TaskClass.WRITE})
    with pytest.raises(UnverifiedWriteError):
        await Harness(Transport()).run_structured(blocked, Output)
