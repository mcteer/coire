from coire_agent.context import prepare_context, truncate_tool_output
from coire_core.models.harness import HarnessMessage


def test_tool_output_keeps_head_and_tail() -> None:
    value, truncated = truncate_tool_output("HEAD" + "x" * 100 + "TAIL", 40)
    assert truncated and value.startswith("HEAD") and value.endswith("TAIL")
    assert "[truncated]" in value


async def test_summary_changes_view_not_original_history() -> None:
    history = [HarnessMessage(role="assistant", content=str(index)) for index in range(8)]

    async def summarize(messages: object) -> str:
        return "older summary"

    view, budget = await prepare_context(
        system_prompt="system",
        task="current",
        history=history,
        token_limit=100,
        reported_prompt_tokens=90,
        summarize=summarize,
        tool_byte_cap=100,
    )
    assert history[0].content == "0"
    assert view[0].role == "system" and view[-1].content == "current"
    assert budget.summarized_messages == 4
