"""Minimal admin API client available only in the ops image."""

from __future__ import annotations

import httpx


class AdminClient:
    def __init__(self, *, api_url: str, token: str) -> None:
        self._url = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def get(self, path: str) -> object:
        async with httpx.AsyncClient(base_url=self._url) as client:
            response = await client.get(path, headers=self._headers)
            response.raise_for_status()
            return response.json()
