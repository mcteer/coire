from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from coire_agent.__main__ import execute, load_request
from coire_core.models.harness import (
    ContextBudget,
    HarnessRunResult,
    ProfileName,
)


def request(variant_id: uuid.UUID, *, profile: str = "general") -> dict[str, Any]:
    return {
        "profile": profile,
        "variant_id": str(variant_id),
        "task_class": "read",
        "task": "answer",
        "history": [],
        "capability_profile": {},
        "context_window": 4096,
        "thinking_token_limit": 10,
    }


async def test_entrypoint_binds_admitted_identity_and_atomically_writes_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, model_id, variant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(json.dumps(request(variant_id)))
    seen: dict[str, object] = {}

    class Transport:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        async def complete_repair(self, *, invalid: str, error: str) -> str:
            return invalid

    class FakeHarness:
        def __init__(self, transport: object, **kwargs: object) -> None:
            seen["harness"] = kwargs

        async def run_structured(self, submitted, output_type):  # type: ignore[no-untyped-def]
            assert submitted.variant_id == variant_id
            return HarnessRunResult(
                run_id=uuid.uuid4(),
                profile=ProfileName.GENERAL,
                variant_id=variant_id,
                output={"answer": "ok"},
                context=ContextBudget(token_limit=4096),
            )

    monkeypatch.setattr("coire_agent.__main__.GatewayTransport", Transport)
    monkeypatch.setattr("coire_agent.__main__.Harness", FakeHarness)
    await execute(
        environ={
            "COIRE_RUN_ID": str(run_id),
            "COIRE_PROFILE": "general",
            "COIRE_MODEL_ID": str(model_id),
            "COIRE_VERIFIED_VARIANT_ID": str(variant_id),
            "COIRE_API_URL": "http://coire-gateway:8080/v1",
            "COIRE_RUN_TOKEN": "secret",
        },
        request_path=request_path,
        result_path=result_path,
    )
    assert seen["model_id"] == str(model_id)
    assert json.loads(result_path.read_text())["run_id"] == str(run_id)
    assert not result_path.with_suffix(".json.tmp").exists()


def test_entrypoint_rejects_oversized_request_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (2 * 1024**2 + 1))
    with pytest.raises(ValueError, match="size"):
        load_request(oversized)
