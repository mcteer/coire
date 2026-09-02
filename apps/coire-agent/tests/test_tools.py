from __future__ import annotations

import pytest

from coire_agent.tools import ToolRegistry


def test_prompted_tool_pack_rejects_nested_union_schema() -> None:
    def ambiguous(value: list[str | int]) -> object:
        return value

    with pytest.raises(ValueError, match="nested union"):
        ToolRegistry().register("bad", {"ambiguous": ambiguous}, prompted=True)


def test_native_tool_pack_can_retain_union_schema() -> None:
    def native(value: str | int) -> object:
        return value

    registry = ToolRegistry()
    registry.register("native", {"native": native})
    assert set(registry.load("native")) == {"native"}
