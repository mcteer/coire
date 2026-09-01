"""Typed read/propose-only API client available only in the ops image."""

from __future__ import annotations

import httpx

from coire_core.models.console import ConsoleSnapshot
from coire_core.models.ops import (
    OpsProposalIssued,
    OpsProposalSubmission,
    OpsSession,
    OpsSessionRegistration,
)


class AdminClient:
    """Narrow client whose public surface intentionally cannot confirm an action."""

    def __init__(
        self,
        *,
        api_url: str,
        token: str,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout_s
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._url,
            headers=self._headers,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def read_snapshot(self) -> ConsoleSnapshot:
        async with self._client() as client:
            response = await client.get("/api/v1/internal/ops/snapshot")
            response.raise_for_status()
            return ConsoleSnapshot.model_validate(response.json())

    async def register_session(self, registration: OpsSessionRegistration) -> OpsSession:
        async with self._client() as client:
            response = await client.post(
                "/api/v1/internal/ops/sessions",
                json=registration.model_dump(mode="json"),
            )
            response.raise_for_status()
            return OpsSession.model_validate(response.json())

    async def heartbeat_session(self, session_id: str) -> OpsSession:
        async with self._client() as client:
            response = await client.patch(f"/api/v1/internal/ops/sessions/{session_id}")
            response.raise_for_status()
            return OpsSession.model_validate(response.json())

    async def submit_proposal(self, submission: OpsProposalSubmission) -> OpsProposalIssued:
        async with self._client() as client:
            response = await client.post(
                "/api/v1/internal/ops/proposals",
                json=submission.model_dump(mode="json"),
            )
            response.raise_for_status()
            return OpsProposalIssued.model_validate(response.json())
