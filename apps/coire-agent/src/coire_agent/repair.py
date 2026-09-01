"""Fail-open adapter for structured-output repair through the pinned admin gateway model."""

from __future__ import annotations

from coire_agent.gateway_model import GatewayTransport


class GatewayRepair:
    def __init__(self, transport: GatewayTransport) -> None:
        self._transport = transport

    async def __call__(self, invalid: str, error: str) -> str | None:
        try:
            return await self._transport.complete_repair(invalid=invalid, error=error)
        except Exception:
            return None
