"""Deterministic image pipeline for local lifecycle and fencing tests only."""

from __future__ import annotations

import threading

from PIL import Image

from coire_core.models.images import ResolvedImageSpec
from coire_node.image_worker import Progress


class FakeImagePipeline:
    """Hold one generation at a step boundary so tests can race node commands."""

    def __init__(self, *, block_at_step: int | None = None) -> None:
        self.block_at_step = block_at_step
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = 0

    def generate(
        self, resolved: ResolvedImageSpec, on_progress: Progress
    ) -> tuple[Image.Image, ...]:
        self.calls += 1
        result: list[Image.Image] = []
        for index, seed in enumerate(resolved.seeds):
            for step in range(1, resolved.spec.steps + 1):
                if step == self.block_at_step:
                    self.entered.set()
                    if not self.release.wait(timeout=10):
                        raise TimeoutError("fake image step was not released")
                on_progress(index, step, resolved.spec.steps)
            result.append(
                Image.new(
                    "RGB",
                    (resolved.spec.width, resolved.spec.height),
                    (seed % 256, (seed >> 8) % 256, (seed >> 16) % 256),
                )
            )
        return tuple(result)
