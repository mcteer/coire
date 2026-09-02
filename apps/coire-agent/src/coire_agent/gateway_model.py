"""Gateway-only model transport; no engine address is accepted by this module."""

from __future__ import annotations

import httpx

from coire_agent.profiles import get_profile
from coire_core.models.harness import HarnessMessage, HarnessRunRequest


class GatewayTransport:
    def __init__(self, *, gateway_url: str, token: str, model_id: str) -> None:
        if not gateway_url.rstrip("/").endswith("/v1"):
            raise ValueError("gateway_url must name Coire's /v1 surface")
        self._url = gateway_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._model_id = model_id

    async def complete(self, messages: list[HarnessMessage], request: HarnessRunRequest) -> object:
        profile = get_profile(request.profile, allow_ops=True)
        body = {
            "model": self._model_id,
            "messages": [
                {
                    "role": message.role if message.role != "summary" else "system",
                    "content": message.content,
                }
                for message in messages
            ],
            "temperature": profile.temperature,
        }
        if profile.stop_sequences:
            body["stop"] = profile.stop_sequences
        if request.thinking_token_limit:
            # Engines count hidden reasoning inside completion usage. A completion ceiling is
            # therefore the only portable hard cap across native and tagged reasoning models.
            body["max_tokens"] = request.thinking_token_limit
        async with httpx.AsyncClient(base_url=self._url, timeout=None) as client:
            response = await client.post("/chat/completions", headers=self._headers, json=body)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]

    async def complete_repair(self, *, invalid: str, error: str) -> str:
        body = {
            "model": self._model_id,
            "messages": [
                {
                    "role": "system",
                    "content": "Repair the JSON. Return JSON only; do not add fields.",
                },
                {"role": "user", "content": f"Error: {error}\nInvalid JSON: {invalid}"},
            ],
            "temperature": 0,
        }
        async with httpx.AsyncClient(base_url=self._url, timeout=None) as client:
            response = await client.post("/chat/completions", headers=self._headers, json=body)
            response.raise_for_status()
            value = response.json()["choices"][0]["message"]["content"]
            if not isinstance(value, str):
                raise TypeError("repair response content must be text")
            return value
