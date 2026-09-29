"""Additive multimodal wire contracts preserve existing text defaults."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from coire_core.models.acquisition import ModelVariant, Precision, VariantRecipe, VariantState
from coire_core.models.engine import EngineStartRequest
from coire_core.models.gateway import ChatMessage, EngineChatRequest
from coire_core.models.harness import HarnessMessage, HarnessRunRequest, ProfileName, TaskClass
from coire_core.models.registry import CapabilityProfile, CapabilityProfileUpdate, EngineBackend
from coire_core.models.runs import RunActivity, RunActivityPage


def test_openai_text_null_and_inline_image_parts() -> None:
    assert ChatMessage(role="assistant").content is None
    assert ChatMessage(role="user", content="hello").content == "hello"
    message = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is here?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        }
    )
    assert len(message.content) == 2
    for url in (
        "https://example.com/a.png",
        "file:///etc/passwd",
        "data:text/html;base64,AA==",
        "data:image/png;base64,AAAAA",
    ):
        with pytest.raises(ValidationError):
            ChatMessage.model_validate(
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}
            )
    with pytest.raises(ValidationError):
        ChatMessage.model_validate({"role": "user", "content": [{"type": "audio", "url": "x"}]})
    assert (
        EngineChatRequest(model="/registry/verified", messages=[message]).messages[0].content
        is not None
    )


def test_backend_and_visual_capability_default_to_text_and_are_server_controlled() -> None:
    request = EngineStartRequest(engine_id=uuid4(), slug="org--model", estimate_bytes=1024)
    assert request.backend is EngineBackend.MLX_LM
    now = datetime.now(UTC)
    variant = ModelVariant(
        id=uuid4(),
        model_id=uuid4(),
        name="4bit",
        precision=Precision.BIT4,
        recipe=VariantRecipe(name="4bit", precision=Precision.BIT4),
        state=VariantState.READY,
        byte_size=0,
        memory_estimate_bytes=0,
        validated=True,
        published=True,
        is_default=True,
        raw_retained=False,
        created_at=now,
        updated_at=now,
    )
    assert variant.backend is EngineBackend.MLX_LM
    assert CapabilityProfile().visual_input is None
    with pytest.raises(ValidationError):
        EngineStartRequest(
            engine_id=uuid4(),
            slug="org--model",
            estimate_bytes=1024,
            backend="mlx_vlm",
            chat_template="x",
        )
    with pytest.raises(ValidationError):
        CapabilityProfileUpdate.model_validate({"visual_input": {"max_images": 10}})


def test_harness_visual_inputs_are_trusted_refs_and_activity_excludes_content() -> None:
    image = {"asset_id": str(uuid4()), "media_type": "image/png", "width": 32, "height": 32}
    request = HarnessRunRequest(
        profile=ProfileName.CODING,
        variant_id=uuid4(),
        task_class=TaskClass.READ,
        task="Inspect diagram",
        capability_profile=CapabilityProfile(),
        context_window=1024,
        visual_inputs=[image],
    )
    assert request.visual_inputs[0].asset_id
    assert HarnessMessage(role="user", content="hi").visual_inputs == []
    with pytest.raises(ValidationError):
        HarnessRunRequest.model_validate(
            {**request.model_dump(), "visual_inputs": [{**image, "path": "/tmp/x"}]}
        )
    activity = RunActivity(
        run_id=uuid4(),
        sequence=1,
        tool_name="read_file",
        state="started",
        created_at=datetime.now(UTC),
    )
    assert RunActivityPage(run_id=activity.run_id, data=[activity]).data[0].sequence == 1
    with pytest.raises(ValidationError):
        RunActivity.model_validate({**activity.model_dump(), "arguments": {"secret": "x"}})
