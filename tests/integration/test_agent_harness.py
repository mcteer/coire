"""Deterministic composed-contract harness checks; no real engine is launched."""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from pydantic import BaseModel

from coire_agent.gateway_model import GatewayTransport
from coire_agent.harness import Harness, UnverifiedWriteError
from coire_agent.strategies import parse_tool_call
from coire_core.models.harness import (
    HarnessMessage,
    HarnessRunRequest,
    HarnessStrategy,
    ProfileName,
    TaskClass,
)
from coire_core.models.registry import CapabilityProfile, StructuredOutput, ToolCalling

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run integration tests",
    ),
]


class Result(BaseModel):
    answer: str


class GatewayDouble:
    calls = 0

    async def complete(self, messages: list[HarnessMessage], request: HarnessRunRequest) -> object:
        self.calls += 1
        return '{"answer":"bounded"}'


def _request(strategy: HarnessStrategy, task_class: TaskClass) -> HarnessRunRequest:
    tool_calling = (
        ToolCalling.NATIVE if strategy is HarnessStrategy.NATIVE else ToolCalling.PROMPTED
    )
    return HarnessRunRequest(
        profile=ProfileName.CODING,
        variant_id=uuid.uuid4(),
        task_class=task_class,
        task="return structured output",
        capability_profile=CapabilityProfile(
            tool_calling=tool_calling,
            structured_output=StructuredOutput.JSON_MODE,
        ),
        context_window=4096,
    )


@pytest.fixture(scope="module")
def composed_model(api_url: str, admin_headers: dict[str, str]) -> str:
    repo_id = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"
    with httpx.Client(base_url=api_url, timeout=30) as client:
        models = client.get("/api/v1/admin/models", headers=admin_headers).json()
        model = next((item for item in models if item["repo_id"] == repo_id), None)
        if model is None:
            response = client.post(
                "/api/v1/admin/models",
                headers=admin_headers,
                json={"repo_id": repo_id, "tags": ["coding"]},
            )
            assert response.status_code == 202, response.text
            model = response.json()
        deadline = time.monotonic() + 120
        while model["state"] not in ("ready", "failed") and time.monotonic() < deadline:
            time.sleep(1)
            model = client.get(f"/api/v1/admin/models/{model['id']}", headers=admin_headers).json()
        assert model["state"] == "ready", model
        response = client.patch(
            f"/api/v1/admin/models/{model['id']}",
            headers={**admin_headers, "If-Match": model["updated_at"]},
            json={"visibility": "published"},
        )
        assert response.status_code == 200, response.text
        return str(model["id"])


async def test_harness_uses_composed_authenticated_gateway(
    api_url: str,
    admin_headers: dict[str, str],
    composed_model: str,
) -> None:
    token = admin_headers["Authorization"].removeprefix("Bearer ")
    transport = GatewayTransport(gateway_url=f"{api_url}/v1", token=token, model_id=composed_model)
    request = _request(HarnessStrategy.NATIVE, TaskClass.READ).model_copy(
        update={"task": "coire-harness-json"}
    )
    result = await Harness(transport).run_structured(request, Result)
    assert result.output == {"answer": "bounded"}


@pytest.mark.parametrize("strategy", list(HarnessStrategy))
async def test_all_strategies_share_one_harness(strategy: HarnessStrategy) -> None:
    gateway = GatewayDouble()
    result = await Harness(gateway).run_structured(_request(strategy, TaskClass.READ), Result)
    assert result.output == {"answer": "bounded"}
    payload: object = {"name": "read_file", "arguments": {"path": "x"}}
    if strategy is HarnessStrategy.JSON:
        payload = '{"name":"read_file","arguments":{"path":"x"}}'
    if strategy is HarnessStrategy.DELIMITED:
        payload = '<tool_call>{"name":"read_file","arguments":{"path":"x"}}</tool_call>'
    assert parse_tool_call(strategy, payload).name == "read_file"


async def test_write_gate_precedes_gateway_invocation() -> None:
    gateway = GatewayDouble()
    with pytest.raises(UnverifiedWriteError):
        await Harness(gateway).run_structured(
            _request(HarnessStrategy.NATIVE, TaskClass.WRITE), Result
        )
    assert gateway.calls == 0
