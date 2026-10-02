"""Node drift includes a measured image child and refuses unknown footprints."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from coire_api.nodes_prober import measured_node_residency
from coire_core.models.engine import EngineState
from coire_core.models.node import NodeStatus


def test_image_worker_footprint_is_included_once_and_unavailable_is_not_zero() -> None:
    status = cast(
        NodeStatus,
        SimpleNamespace(
            engines=[
                SimpleNamespace(resident_bytes=100, state=EngineState.READY),
                SimpleNamespace(resident_bytes=None, state=EngineState.STOPPED),
            ],
            image_worker_resident_bytes=50,
        ),
    )
    assert measured_node_residency(status, image_reserved_bytes=200) == 150
    status.image_worker_resident_bytes = None
    assert measured_node_residency(status, image_reserved_bytes=200) is None
    assert measured_node_residency(status, image_reserved_bytes=0) == 100
    status.engines[1].state = EngineState.READY
    assert measured_node_residency(status, image_reserved_bytes=0) is None
