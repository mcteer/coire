"""Require the actual Linux ARM64 container incarnation before recovery effects."""

import platform
import sys

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import CoreBootstrapSeed


def verify_executor_namespace(seed: CoreBootstrapSeed) -> None:
    if (
        sys.platform != "linux"
        or platform.machine() not in {"aarch64", "arm64"}
        or platform.node() != seed.executor_container_id[:12]
    ):
        raise UpgradeUnavailable("Initial bootstrap is outside its exact executor container")
