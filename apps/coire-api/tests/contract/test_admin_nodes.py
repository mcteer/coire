"""Admin data-link response contract."""

from __future__ import annotations

from datetime import UTC, datetime

from coire_api.auth import ADMIN
from coire_api.routes.admin_nodes import studio_data_link
from coire_core.models.link import LinkState, RdmaState, StudioDataLinkStatus


class Client:
    async def data_link_status(self, node: str) -> StudioDataLinkStatus:
        assert node == "coire-edge-a"
        return StudioDataLinkStatus(
            node_a="coire-edge-a",
            node_b="coire-edge-b",
            ip_state=LinkState.UP,
            rdma_state=RdmaState.UP,
            bandwidth_bytes_per_second=1_500_000_000,
            latency_ms=0.85,
            measured_at=datetime.now(UTC),
        )


async def test_admin_link_surface_is_typed() -> None:
    response = await studio_data_link(ADMIN, Client())  # type: ignore[arg-type]
    assert response.ip_state is LinkState.UP
    assert response.rdma_state is RdmaState.UP
    assert response.node_a < response.node_b
