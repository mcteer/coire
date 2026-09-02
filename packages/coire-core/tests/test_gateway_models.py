from coire_core.models.gateway import EngineChatRequest


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
