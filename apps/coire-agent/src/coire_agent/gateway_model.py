"""Gateway-only model transport; no engine address is accepted by this module."""

from __future__ import annotations

import asyncio
import base64
import os
import stat
from pathlib import Path

import httpx

from coire_agent.profiles import get_profile
from coire_core.models.harness import HarnessMessage, HarnessRunRequest

CONTROL_IMAGE_ROOT = Path("/workspace/.coire/inputs")
MAX_CONTROL_IMAGE_BYTES = 10 * 1024 * 1024


def _read_control_image(asset_id: str, width: int, height: int) -> bytes:
    """Read only a generated UUID PNG from the node's read-only control mount."""
    path = CONTROL_IMAGE_ROOT / f"{asset_id}.png"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("visual control input unavailable") from exc
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= MAX_CONTROL_IMAGE_BYTES
        ):
            raise ValueError("visual control input exceeds its bound")
        data = source.read(MAX_CONTROL_IMAGE_BYTES + 1)
    if (
        len(data) != metadata.st_size
        or len(data) < 24
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[12:16] != b"IHDR"
        or int.from_bytes(data[16:20], "big") != width
        or int.from_bytes(data[20:24], "big") != height
    ):
        raise ValueError("visual control input changed")
    return data


async def _message_content(message: HarnessMessage) -> object:
    if not message.visual_inputs:
        return message.content
    parts: list[dict[str, object]] = [{"type": "text", "text": message.content}]
    for image in message.visual_inputs:
        if image.media_type != "image/png" or message.role != "user":
            raise ValueError("visual control input requires a normalized user PNG")
        data = await asyncio.to_thread(
            _read_control_image, str(image.asset_id), image.width, image.height
        )
        parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")
                },
            }
        )
    return parts


class GatewayTransport:
    def __init__(self, *, gateway_url: str, token: str, model_id: str) -> None:
        if not gateway_url.rstrip("/").endswith("/v1"):
            raise ValueError("gateway_url must name Coire's /v1 surface")
        self._url = gateway_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._model_id = model_id

    async def complete(self, messages: list[HarnessMessage], request: HarnessRunRequest) -> object:
        profile = get_profile(request.profile, allow_ops=True)
        wire_messages = [
            {
                "role": message.role if message.role != "summary" else "system",
                "content": await _message_content(message),
            }
            for message in messages
        ]
        body = {
            "model": self._model_id,
            "messages": wire_messages,
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
