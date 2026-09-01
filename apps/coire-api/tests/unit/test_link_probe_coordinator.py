from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from coire_api.db import LinkObservationRow
from coire_api.link_probe_coordinator import versions_need_probe
from coire_core.models import ProbeTransport


def row(transport: ProbeTransport, versions: tuple[str, str, str]) -> LinkObservationRow:
    return cast(
        LinkObservationRow,
        cast(
            Any,
            SimpleNamespace(
                transport=transport,
                os_version_a=versions[0],
                os_version_b=versions[1],
                engine_version=versions[2],
            ),
        ),
    )


def test_first_boot_and_partial_evidence_require_probe() -> None:
    signature = ("26.0", "26.0", "mlx-lm-1")
    assert versions_need_probe([], signature)
    assert versions_need_probe([row(ProbeTransport.JACCL, signature)], signature)


def test_complete_matching_evidence_is_current_but_upgrade_reprobes() -> None:
    signature = ("26.0", "26.0", "mlx-lm-1")
    observations = [
        row(ProbeTransport.JACCL, signature),
        row(ProbeTransport.RING, signature),
    ]
    assert not versions_need_probe(observations, signature)
    assert versions_need_probe(observations, ("26.1", "26.0", "mlx-lm-1"))
    assert versions_need_probe(observations, ("26.0", "26.0", "mlx-lm-2"))
