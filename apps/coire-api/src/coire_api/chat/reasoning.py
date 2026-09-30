"""Incremental, profile-gated separation of thinking text from answer text."""

from __future__ import annotations

from typing import Literal

from coire_core.models.registry import Reasoning

Channel = Literal["answer", "reasoning"]
OPEN = "<think>"
CLOSE = "</think>"


class ReasoningParser:
    def __init__(self, mode: Reasoning) -> None:
        self.enabled = mode in {Reasoning.THINKING, Reasoning.HYBRID}
        self.inside = False
        self.pending = ""

    def feed(self, content: str) -> list[tuple[Channel, str]]:
        if not content:
            return []
        if not self.enabled:
            return [("answer", content)]
        self.pending += content
        result: list[tuple[Channel, str]] = []
        while self.pending:
            marker = CLOSE if self.inside else OPEN
            lower = self.pending.lower()
            at = lower.find(marker)
            if at >= 0:
                if at:
                    result.append(("reasoning" if self.inside else "answer", self.pending[:at]))
                self.pending = self.pending[at + len(marker) :]
                self.inside = not self.inside
                continue
            held = 0
            for length in range(min(len(marker) - 1, len(self.pending)), 0, -1):
                if lower.endswith(marker[:length]):
                    held = length
                    break
            if len(self.pending) > held:
                result.append(
                    (
                        "reasoning" if self.inside else "answer",
                        self.pending[: len(self.pending) - held],
                    )
                )
            self.pending = self.pending[len(self.pending) - held :] if held else ""
            break
        return result

    def finish(self) -> list[tuple[Channel, str]]:
        if not self.pending:
            return []
        # An unfinished opening marker is withheld from the answer, while an
        # unfinished closing marker belongs to the saved reasoning channel.
        result: list[tuple[Channel, str]] = []
        if self.inside:
            result.append(("reasoning", self.pending))
        self.pending = ""
        return result
