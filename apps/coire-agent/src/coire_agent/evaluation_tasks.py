"""Declarative scorers and in-memory patches; never execute candidate code."""

from __future__ import annotations

import json
import re

from coire_core.models.evaluation import MAX_CASE_BYTES, EvaluationAssertion, EvaluationCase

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)")
_ALLOWED = frozenset(
    {("--- a/fixture.py", "+++ b/fixture.py"), ("--- a/note.txt", "+++ b/note.txt")}
)


def apply_fixture_patch(fixture: str, patch: str) -> str:
    if len(patch.encode()) > MAX_CASE_BYTES:
        raise ValueError("patch exceeds bound")
    lines = patch.splitlines(keepends=True)
    if len(lines) < 3 or tuple(line.rstrip("\n") for line in lines[:2]) not in _ALLOWED:
        raise ValueError("patch must name one fixed authored fixture")
    source = fixture.splitlines(keepends=True)
    result: list[str] = []
    cursor = 0
    index = 2
    while index < len(lines):
        header = _HUNK.fullmatch(lines[index].rstrip("\n"))
        if header is None:
            raise ValueError("invalid patch hunk")
        old_start, old_count, new_start, new_count = header.groups()
        start = int(old_start) - 1 if int(old_start) else 0
        old_total = int(old_count) if old_count is not None else 1
        new_total = int(new_count) if new_count is not None else 1
        if start < cursor or start > len(source):
            raise ValueError("overlapping or missing patch location")
        result.extend(source[cursor:start])
        cursor = start
        if int(new_start) != len(result) + 1:
            raise ValueError("new patch location differs")
        index += 1
        removed = added = 0
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if not line or line[0] not in " +-":
                raise ValueError("unsupported patch operation")
            operation, content = line[0], line[1:]
            if operation in " -":
                if cursor >= len(source) or source[cursor] != content:
                    raise ValueError("patch context differs from fixture")
                cursor += 1
                removed += 1
            if operation in " +":
                result.append(content)
                added += 1
            index += 1
        if removed != old_total or added != new_total:
            raise ValueError("patch hunk counts differ")
    result.extend(source[cursor:])
    return "".join(result)


def _assert(assertion: EvaluationAssertion, output: str) -> bool:
    if assertion.kind == "exact":
        return output == assertion.value
    if assertion.kind in {"contains", "retrieval"}:
        return assertion.value in output
    if assertion.kind == "patch":
        return (
            assertion.fixture is not None
            and apply_fixture_patch(assertion.fixture, output) == assertion.value
        )
    value = json.loads(output)
    if assertion.kind == "json_object":
        return isinstance(value, dict) and bool(value)
    if assertion.kind == "tool_call":
        return (
            isinstance(value, dict)
            and set(value) == {"name", "arguments"}
            and value["name"] == assertion.value
            and value["arguments"] == {"path": "README.md"}
        )
    return False


def score_case(case: EvaluationCase, output: str) -> bool:
    if len(output.encode()) > MAX_CASE_BYTES:
        return False
    try:
        return bool(case.assertions) and all(
            _assert(assertion, output) for assertion in case.assertions
        )
    except (ValueError, TypeError, KeyError):
        return False
