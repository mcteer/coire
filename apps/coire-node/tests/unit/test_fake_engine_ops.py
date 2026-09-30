"""The CI engine double must follow the current ops tool loop."""

from __future__ import annotations

import json

from coire_node.testing.fake_engine import Handler


def test_prompted_ops_turn_proposes_from_the_compact_snapshot() -> None:
    snapshot = {
        "active_instances": [
            {
                "id": "instance-1",
                "state": "ready",
                "updated_at": "2026-09-30T00:00:00Z",
            }
        ]
    }
    completion = Handler._ops_completion(
        Handler,  # type: ignore[arg-type]
        {"messages": [{"role": "tool", "content": json.dumps(snapshot)}]},
        prompted=True,
    )
    call = completion["choices"][0]["message"]["tool_calls"][0]["function"]  # type: ignore[index]
    assert call["name"] == "propose_reversible_action"
    arguments = json.loads(call["arguments"])
    assert arguments["action"]["target_id"] == "instance-1"


def test_prompted_ops_turn_finishes_as_json_after_the_proposal() -> None:
    completion = Handler._ops_completion(
        Handler,  # type: ignore[arg-type]
        {"messages": [{"role": "tool", "content": "Proposal staged for human confirmation."}]},
        prompted=True,
    )
    message = completion["choices"][0]["message"]  # type: ignore[index]
    assert message["content"] == (
        '{"answer": "Prepared one exact reversible proposal for review."}'
    )
    assert "tool_calls" not in message
    assert completion["choices"][0]["finish_reason"] == "stop"  # type: ignore[index]
