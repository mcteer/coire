"""Paired upgrade results, separate from engine authority imports."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from coire_core.models.sharding import LinkObservation
from coire_core.models.upgrades import Sha256, UpgradeId, UpgradeWire


class NodeUpgradeCollectiveResult(UpgradeWire):
    job_id: UpgradeId
    generation: int = Field(ge=1, le=2**63 - 1)
    plan_sha256: Sha256
    stage: Literal["candidate", "source"]
    deadline_at: AwareDatetime
    observations: tuple[LinkObservation, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def bounded_epoch(self) -> "NodeUpgradeCollectiveResult":
        for observation in self.observations:
            if (
                observation.observed_at.tzinfo is None
                or observation.observed_at.utcoffset() is None
                or observation.observed_at >= self.deadline_at
                or observation.transport.value != "jaccl"
                or (observation.node_a, observation.node_b) != ("coire-edge-a", "coire-edge-b")
            ):
                raise ValueError("Collective result is outside its fixed pair or original epoch")
        return self
