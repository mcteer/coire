from __future__ import annotations

import json

import httpx
import pytest

from coire_node.docker_api import DockerAPI, DockerAPIError


@pytest.mark.anyio
async def test_docker_api_uses_versioned_typed_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        routes = {
            ("POST", "/v1.46/networks/create"): (201, {"Id": "net-1"}),
            ("POST", "/v1.46/containers/create"): (201, {"Id": "ctr-1"}),
            ("POST", "/v1.46/containers/ctr-1/start"): (204, None),
            ("POST", "/v1.46/containers/ctr-1/wait"): (200, {"StatusCode": 0}),
            ("DELETE", "/v1.46/containers/ctr-1"): (204, None),
            ("DELETE", "/v1.46/networks/net-1"): (204, None),
        }
        status, body = routes[(request.method, request.url.path)]
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://docker"
    ) as client:
        api = DockerAPI("/ignored", client=client)
        network = await api.create_network("coire-run-1")
        container = await api.create_container("coire-run-1", {"Image": "digest"})
        await api.start_container(container)
        assert await api.wait_container(container) == 0
        await api.remove_container(container)
        await api.remove_network(network)

    assert json.loads(seen[0].content)["Internal"] is True
    assert seen[1].url.params["name"] == "coire-run-1"


@pytest.mark.anyio
async def test_docker_error_is_bounded_and_does_not_expose_request_payload() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"message": "x" * 1000})
        ),
        base_url="http://docker",
    ) as client:
        api = DockerAPI("/ignored", client=client)
        with pytest.raises(DockerAPIError) as caught:
            await api.create_container("run", {"Env": ["RUN_TOKEN=secret"]})
    assert "secret" not in str(caught.value)
    assert len(str(caught.value)) < 400


@pytest.mark.anyio
async def test_inspect_and_archive_treat_not_found_as_absent() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        base_url="http://docker",
    ) as client:
        api = DockerAPI("/ignored", client=client)
        assert await api.inspect_container("missing") is None
        assert await api.archive("missing", "/workspace/.coire/result.json") is None
