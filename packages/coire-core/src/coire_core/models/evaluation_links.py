"""Content-free links keep evaluation status separate from training and verification."""

import uuid
from typing import Literal

from pydantic import Field

from coire_core.models.training_types import TrainingId, TrainingWire


class EvaluationGroupLink(TrainingWire):
    trigger_id: uuid.UUID | None = None
    group_id: TrainingId | None = None
    origin: Literal["manual", "training_final", "training_checkpoint", "measurement"]
    checkpoint_id: uuid.UUID | None = None
    completed_update: int | None = Field(default=None, ge=0, le=100_000)
    attempt_id: TrainingId | None = Field(default=None, exclude_if=lambda value: value is None)
    fence: int | None = Field(default=None, ge=1, exclude_if=lambda value: value is None)
    trigger_phase: (
        Literal[
            "pending_pause",
            "pending_admission",
            "preparing_adapter",
            "evaluating",
            "cleaning_adapter",
            "resume_pending",
            "complete",
        ]
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    pause_owner: Literal["evaluation", "admin", "protective", "released"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    resume_disposition: str | None = Field(
        default=None, max_length=32, exclude_if=lambda value: value is None
    )
    state: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    run_ids: list[TrainingId] = Field(default_factory=list, max_length=4)
    result_ids: list[TrainingId] = Field(default_factory=list, max_length=4)
