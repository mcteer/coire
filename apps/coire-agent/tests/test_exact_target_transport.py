from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from coire_agent.__main__ import execute
from coire_agent.gateway_model import GatewayTransport
from coire_agent.harness import Harness, UnverifiedWriteError
from coire_core.models.adapters import InferenceTarget
from coire_core.models.harness import HarnessMessage, HarnessRunRequest


def target() -> InferenceTarget:
    return InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        adapter_id=uuid.uuid4(),
        base_manifest_sha256="a" * 64,
        adapter_manifest_sha256="b" * 64,
    )


def request(subject: InferenceTarget, task_class: str = "read") -> HarnessRunRequest:
    return HarnessRunRequest.model_validate(
        {
            "profile": "general",
            "variant_id": subject.variant_id,
            "target": subject,
            "task_class": task_class,
            "task": "Answer",
            "capability_profile": {},
            "context_window": 4096,
        }
    )


async def test_generation_and_repair_route_the_same_exact_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = target()
    bodies: list[dict[str, object]] = []

    def respond(wire: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(wire.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok"}'}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "coire_agent.gateway_model.httpx.AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)),
    )
    transport = GatewayTransport(
        gateway_url="http://gateway/v1",
        token="fixture",
        model_id=f"{subject.model_id}@adapter-one",
        target=subject,
    )
    await transport.complete([HarnessMessage(role="user", content="Answer")], request(subject))
    await transport.complete_repair(invalid="broken", error="invalid JSON")
    assert len(bodies) == 2
    assert all(
        body["model"] == f"{subject.model_id}@adapter-one"
        and body["coire_variant_id"] == str(subject.variant_id)
        for body in bodies
    )
    with pytest.raises(ValueError, match="admitted"):
        await transport.complete(
            [], request(subject.model_copy(update={"adapter_id": uuid.uuid4()}))
        )
    assert len(bodies) == 2


async def test_verified_base_callback_cannot_verify_adapter() -> None:
    subject = target()

    class Output(BaseModel):
        answer: str

    class Transport:
        async def complete(self, messages: list[HarnessMessage], request: HarnessRunRequest) -> str:
            return '{"answer":"ok"}'

    async def base_verified(variant: uuid.UUID) -> bool:
        return variant == subject.variant_id

    harness = Harness(Transport(), verify_variant=base_verified)
    with pytest.raises(UnverifiedWriteError):
        await harness.run_structured(request(subject, "write"), Output)

    async def exact_verified(candidate: InferenceTarget) -> bool:
        return candidate == subject

    harness = Harness(Transport(), verify_variant=base_verified, verify_target=exact_verified)
    result = await harness.run_structured(request(subject, "write"), Output)
    assert result.target == subject
    with pytest.raises(UnverifiedWriteError):
        await harness.run_structured(
            request(subject.model_copy(update={"adapter_id": uuid.uuid4()}), "write"), Output
        )


def test_pair_cannot_be_replaced_with_parent_selector() -> None:
    subject = target()
    with pytest.raises(ValueError, match="adapter"):
        GatewayTransport(
            gateway_url="http://gateway/v1",
            token="fixture",
            model_id=str(subject.model_id),
            target=subject,
        )


@pytest.mark.parametrize("missing_target", [True, False])
async def test_entrypoint_refuses_unbound_or_substituted_adapter(
    tmp_path: Path, missing_target: bool
) -> None:
    admitted = target()
    submitted = request(admitted).model_dump(mode="json")
    submitted["target"] = (
        None
        if missing_target
        else admitted.model_copy(update={"adapter_id": uuid.uuid4()}).model_dump(mode="json")
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(submitted))
    with pytest.raises(ValueError, match="admitted exact target"):
        await execute(
            environ={
                "COIRE_RUN_ID": str(uuid.uuid4()),
                "COIRE_PROFILE": "general",
                "COIRE_MODEL_ID": str(admitted.model_id),
                "COIRE_VERIFIED_VARIANT_ID": str(admitted.variant_id),
                "COIRE_INFERENCE_TARGET": admitted.model_dump_json(),
            },
            request_path=path,
            result_path=tmp_path / "result.json",
        )
    assert not (tmp_path / "result.json").exists()


@pytest.mark.parametrize("legacy_base_request", [False, True])
async def test_entrypoint_transports_and_records_admitted_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_base_request: bool
) -> None:
    admitted = target()
    if legacy_base_request:
        admitted = admitted.model_copy(update={"adapter_id": None, "adapter_manifest_sha256": None})
    submitted = request(admitted, "write").model_dump(mode="json")
    if legacy_base_request:
        submitted["target"] = None
    path = tmp_path / "request.json"
    path.write_text(json.dumps(submitted))
    bodies: list[dict[str, object]] = []

    def respond(wire: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(wire.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"answer":"exact"}'}}]}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "coire_agent.gateway_model.httpx.AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)),
    )
    selector = str(admitted.model_id) if legacy_base_request else f"{admitted.model_id}@trained"
    result_path = tmp_path / "result.json"
    await execute(
        environ={
            "COIRE_RUN_ID": str(uuid.uuid4()),
            "COIRE_PROFILE": "general",
            "COIRE_MODEL_ID": str(admitted.model_id),
            "COIRE_VERIFIED_VARIANT_ID": str(admitted.variant_id),
            "COIRE_INFERENCE_TARGET": admitted.model_dump_json(),
            "COIRE_PUBLIC_SELECTOR": selector,
            "COIRE_HARNESS_VERIFIED": "true",
            "COIRE_API_URL": "http://gateway/v1",
            "COIRE_RUN_TOKEN": "fixture",
        },
        request_path=path,
        result_path=result_path,
    )
    assert len(bodies) == 1 and bodies[0]["model"] == selector
    assert bodies[0]["coire_variant_id"] == str(admitted.variant_id)
    assert InferenceTarget.model_validate(json.loads(result_path.read_text())["target"]) == admitted
