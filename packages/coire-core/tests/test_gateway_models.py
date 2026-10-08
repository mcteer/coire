import uuid

import pytest
from pydantic import ValidationError

from coire_core.models.gateway import ChatCompletionRequest, ChatMessage, EngineChatRequest


@pytest.mark.parametrize("seed", [0, 2**32 - 1])
def test_seed_survives_public_to_engine_contract(seed: int) -> None:
    public = ChatCompletionRequest(
        model=str(uuid.uuid4()), messages=[ChatMessage(role="user", content="hello")], seed=seed
    )
    payload = public.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"coire_wait_for_model", "coire_affinity_node", "coire_variant_id"},
    )
    payload["model"] = "/registry/verified"
    engine = EngineChatRequest.model_validate(payload)
    assert engine.model_dump(mode="json", exclude_none=True)["seed"] == seed


@pytest.mark.parametrize("seed", [-1, 2**32])
def test_engine_seed_rejects_out_of_range_values(seed: int) -> None:
    with pytest.raises(ValidationError):
        EngineChatRequest.model_validate(
            {"model": "verified", "messages": [{"role": "user", "content": "hello"}], "seed": seed}
        )


def test_public_chat_contract_preserves_standard_stream_usage_option() -> None:
    request = ChatCompletionRequest.model_validate(
        {
            "model": str(uuid.uuid4()),
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    )
    assert request.model_dump(mode="json", exclude_none=True)["stream_options"] == {
        "include_usage": True
    }


def test_engine_contract_preserves_usage_request() -> None:
    request = EngineChatRequest.model_validate(
        {
            "model": "/registry/verified",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    )
    assert request.model_dump(mode="json", exclude_none=True)["stream_options"] == {
        "include_usage": True
    }


def test_engine_contract_accepts_assistant_tool_call_without_content() -> None:
    request = EngineChatRequest.model_validate(
        {
            "model": "/opt/coire/models/example",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read_snapshot", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
            ],
        }
    )

    assert request.messages[0].content is None
    assert request.messages[0].model_extra == {
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_snapshot", "arguments": "{}"},
            }
        ]
    }
