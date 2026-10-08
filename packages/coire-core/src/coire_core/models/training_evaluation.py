"""V2 schedule value types; no imports of training document classes."""

from __future__ import annotations

from pydantic import Field, model_validator

from coire_core.models.evaluation import EvaluationSuite
from coire_core.models.training_types import AdapterSlug, TrainingWire


class TrainingSuiteSchedule(TrainingWire):
    suite_id: AdapterSlug
    suite_version: int = Field(strict=True, ge=1, le=2**31 - 1)
    checkpoint_updates: list[int] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def ordered_updates(self) -> TrainingSuiteSchedule:
        if any(
            type(update) is not int or not 1 <= update <= 100_000
            for update in self.checkpoint_updates
        ) or self.checkpoint_updates != sorted(set(self.checkpoint_updates)):
            raise ValueError("evaluation checkpoints must be sorted unique positive updates")
        return self


class ResolvedTrainingSuite(TrainingWire):
    schedule: TrainingSuiteSchedule
    suite: EvaluationSuite

    @model_validator(mode="after")
    def matches_schedule(self) -> ResolvedTrainingSuite:
        if (self.schedule.suite_id, self.schedule.suite_version) != (
            self.suite.suite_id,
            self.suite.version,
        ):
            raise ValueError("resolved suite differs from declared schedule")
        return self
