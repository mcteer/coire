"""Derive managed changes from frozen identities; never trust a caller cohort."""

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    COHORT_ORDER,
    ServiceName,
    UpgradeKind,
    UpgradeReleaseManifest,
)


def changed_cohort(
    source: UpgradeReleaseManifest, target: UpgradeReleaseManifest
) -> tuple[ServiceName, ...]:
    if source.kind is not target.kind or source.platform != target.platform:
        raise UpgradeConflict("Release target platform differs")
    if target.kind is UpgradeKind.STUDIO:
        return ()
    if source.project != target.project:
        raise UpgradeConflict("Release project differs")
    previous = {service.service: service for service in source.services}
    candidate = {service.service: service for service in target.services}
    if previous.keys() != candidate.keys():
        raise UpgradeConflict("Managed rollout cannot add or remove service roles")
    return tuple(
        name
        for name in COHORT_ORDER
        if name in candidate
        and (
            candidate[name].image_id != previous[name].image_id
            or candidate[name].configuration_sha256 != previous[name].configuration_sha256
        )
    )
