"""Minimal typed Docker Engine API client over a node-local Unix socket."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx


class DockerAPIError(RuntimeError):
    def __init__(self, status_code: int, operation: str, detail: str = "") -> None:
        self.status_code = status_code
        self.operation = operation
        super().__init__(f"Docker {operation} failed ({status_code}): {detail[:300]}")


class DockerAPI:
    """The small Engine API surface required by run orchestration."""

    def __init__(
        self,
        socket_path: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_version: str = "v1.46",
    ) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://docker",
            timeout=httpx.Timeout(30.0, read=None),
        )
        self._prefix = f"/{api_version}"

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, expected: tuple[int, ...], **kwargs: Any
    ) -> httpx.Response:
        response = await self._client.request(method, f"{self._prefix}{path}", **kwargs)
        if response.status_code not in expected:
            detail = response.text
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = str(body.get("message", detail))
            except (ValueError, json.JSONDecodeError):
                pass
            raise DockerAPIError(response.status_code, f"{method} {path}", detail)
        return response

    async def create_network(self, name: str, *, internal: bool = True) -> str:
        response = await self._request(
            "POST",
            "/networks/create",
            expected=(201,),
            json={"Name": name, "Driver": "bridge", "Internal": internal, "CheckDuplicate": True},
        )
        return str(response.json()["Id"])

    async def remove_network(self, network_id: str) -> None:
        await self._request("DELETE", f"/networks/{network_id}", expected=(204, 404))

    async def inspect_network(self, network_id: str) -> dict[str, Any] | None:
        response = await self._request("GET", f"/networks/{network_id}", expected=(200, 404))
        if response.status_code == 404:
            return None
        body = response.json()
        if not isinstance(body, dict):
            raise DockerAPIError(502, "inspect network", "non-object response")
        return body

    async def connect_network(
        self, network_id: str, container_id: str, *, aliases: list[str] | None = None
    ) -> None:
        await self._request(
            "POST",
            f"/networks/{network_id}/connect",
            expected=(200, 403),
            json={
                "Container": container_id,
                "EndpointConfig": {"Aliases": aliases or []},
            },
        )

    async def create_container(self, name: str, payload: Mapping[str, Any]) -> str:
        response = await self._request(
            "POST", "/containers/create", expected=(201,), params={"name": name}, json=payload
        )
        return str(response.json()["Id"])

    async def start_container(self, container_id: str) -> None:
        await self._request("POST", f"/containers/{container_id}/start", expected=(204, 304))

    async def inspect_container(self, container_id: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET", f"/containers/{container_id}/json", expected=(200, 404)
        )
        if response.status_code == 404:
            return None
        body = response.json()
        if not isinstance(body, dict):
            raise DockerAPIError(502, "inspect", "non-object response")
        return body

    async def wait_container(self, container_id: str, *, condition: str = "not-running") -> int:
        response = await self._request(
            "POST",
            f"/containers/{container_id}/wait",
            expected=(200,),
            params={"condition": condition},
        )
        return int(response.json()["StatusCode"])

    async def logs(self, container_id: str, *, since: int = 0) -> bytes:
        response = await self._request(
            "GET",
            f"/containers/{container_id}/logs",
            expected=(200,),
            params={"stdout": True, "stderr": True, "timestamps": True, "since": since},
        )
        return response.content

    async def stats(self, container_id: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            f"/containers/{container_id}/stats",
            expected=(200, 404),
            params={"stream": False, "one-shot": True},
        )
        if response.status_code == 404:
            return None
        body = response.json()
        if not isinstance(body, dict):
            raise DockerAPIError(502, "stats", "non-object response")
        return body

    async def archive(self, container_id: str, path: str) -> bytes | None:
        response = await self._request(
            "GET",
            f"/containers/{container_id}/archive",
            expected=(200, 404),
            params={"path": path},
        )
        return None if response.status_code == 404 else response.content

    async def kill_container(self, container_id: str, *, signal: str = "KILL") -> None:
        await self._request(
            "POST",
            f"/containers/{container_id}/kill",
            expected=(204, 304, 404, 409),
            params={"signal": signal},
        )

    async def remove_container(self, container_id: str, *, force: bool = True) -> None:
        await self._request(
            "DELETE",
            f"/containers/{container_id}",
            expected=(204, 404),
            params={"force": force, "v": True},
        )

    async def list_containers(self, *, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        filters = {"label": [f"{key}={value}" for key, value in labels.items()]}
        response = await self._request(
            "GET",
            "/containers/json",
            expected=(200,),
            params={"all": True, "filters": json.dumps(filters, separators=(",", ":"))},
        )
        body = response.json()
        if not isinstance(body, list):
            raise DockerAPIError(502, "list", "non-list response")
        return [item for item in body if isinstance(item, dict)]

    async def stream_logs(self, container_id: str, *, since: int = 0) -> AsyncIterator[bytes]:
        async with self._client.stream(
            "GET",
            f"{self._prefix}/containers/{container_id}/logs",
            params={"stdout": True, "stderr": True, "follow": True, "since": since},
        ) as response:
            if response.status_code != 200:
                detail = (await response.aread()).decode("utf-8", errors="replace")
                raise DockerAPIError(response.status_code, "stream logs", detail)
            async for chunk in response.aiter_bytes():
                yield chunk
