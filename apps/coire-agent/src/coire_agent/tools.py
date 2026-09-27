"""On-demand flat tool packs with schema guards."""

from __future__ import annotations

import inspect
from collections.abc import Callable

from pydantic import TypeAdapter

Tool = Callable[..., object]


class ToolRegistry:
    def __init__(self) -> None:
        self._packs: dict[str, dict[str, Tool]] = {}

    def register(self, name: str, tools: dict[str, Tool], *, prompted: bool = False) -> None:
        if len(tools) > 10 or len(tools) != len(set(tools)):
            raise ValueError("tool packs contain at most ten uniquely named tools")
        if prompted:
            for tool_name, tool in tools.items():
                for parameter in inspect.signature(tool).parameters.values():
                    if parameter.annotation is inspect.Parameter.empty:
                        continue
                    schema = TypeAdapter(parameter.annotation).json_schema()
                    if _contains_nested_union(schema):
                        raise ValueError(
                            f"prompted tool {tool_name} contains a nested union schema"
                        )
        self._packs[name] = dict(tools)

    def load(self, name: str) -> dict[str, Tool]:
        if name not in self._packs:
            raise KeyError(f"unknown tool pack: {name}")
        return dict(self._packs[name])


def _contains_nested_union(value: object, *, depth: int = 0) -> bool:
    if isinstance(value, dict):
        if depth > 0 and ("anyOf" in value or "oneOf" in value):
            return True
        return any(_contains_nested_union(child, depth=depth + 1) for child in value.values())
    if isinstance(value, list):
        return any(_contains_nested_union(child, depth=depth + 1) for child in value)
    return False
