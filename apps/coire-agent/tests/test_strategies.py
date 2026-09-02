import pytest

from coire_agent.strategies import ToolParseError, parse_tool_call, strip_reasoning
from coire_core.models.harness import HarnessStrategy


@pytest.mark.parametrize(
    ("strategy", "payload"),
    [
        (HarnessStrategy.NATIVE, {"name": "search", "arguments": {"q": "x"}}),
        (HarnessStrategy.JSON, '{"name":"search","arguments":{"q":"x"}}'),
        (
            HarnessStrategy.DELIMITED,
            '<think>private</think><tool_call>{"name":"search","arguments":{"q":"x"}}</tool_call>',
        ),
    ],
)
def test_all_strategies_normalize(strategy: HarnessStrategy, payload: object) -> None:
    call = parse_tool_call(strategy, payload)
    assert call.name == "search"
    assert call.arguments == {"q": "x"}


def test_reasoning_is_separate() -> None:
    clean, reasoning = strip_reasoning("<think>work</think>answer")
    assert clean == "answer"
    assert reasoning == "work"


def test_malformed_delimited_call_is_specific() -> None:
    with pytest.raises(ToolParseError, match="delimiter"):
        parse_tool_call(HarnessStrategy.DELIMITED, "{}")
