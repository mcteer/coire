"""Private launcher diagnostics; these are never execution or cleanup authority."""

from pydantic import AwareDatetime, Field

from coire_core.models.sharding import LinkProbeCommand
from coire_core.models.upgrades import UpgradeWire


class ProbeLauncherDiagnostic(UpgradeWire):
    command: LinkProbeCommand
    argv_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    returncode: int
    output_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_bytes: int = Field(ge=0)
    output_tail: str = Field(max_length=8192)
    truncated: bool
    observed_at: AwareDatetime
