"""Small shared scalar constraints for dataset, training and artifact contracts."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from coire_core.models.files import SHA256_PATTERN, ULID_PATTERN

type Digest = Annotated[str, Field(pattern=SHA256_PATTERN)]
type TrainingId = Annotated[str, Field(pattern=ULID_PATTERN)]
type AdapterSlug = Annotated[
    str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
]
type StudioName = Annotated[str, Field(pattern=r"^coire-edge-[ab]$")]
type Seed = Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]
type PositiveCount = Annotated[int, Field(strict=True, ge=1)]


class TrainingWire(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
