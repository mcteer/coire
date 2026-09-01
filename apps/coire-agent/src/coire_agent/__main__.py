"""Strict workspace-request entrypoint for the ephemeral Studio harness image."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path

from coire_agent.gateway_model import GatewayTransport
from coire_agent.harness import Harness
from coire_agent.pydantic_runtime import OUTPUT_TYPES
from coire_core.models.harness import HarnessRunRequest, ProfileName

REQUEST_PATH = Path("/workspace/.coire/request.json")
RESULT_PATH = Path("/workspace/.coire/result.json")
MAX_REQUEST_BYTES = 2 * 1024**2


def load_request(path: Path = REQUEST_PATH) -> HarnessRunRequest:
    size = path.stat().st_size
    if size <= 0 or size > MAX_REQUEST_BYTES:
        raise ValueError("harness request size is invalid")
    return HarnessRunRequest.model_validate(json.loads(path.read_bytes()))


async def execute(
    *,
    environ: Mapping[str, str] | None = None,
    request_path: Path = REQUEST_PATH,
    result_path: Path = RESULT_PATH,
) -> None:
    env = environ or os.environ
    run_id = uuid.UUID(env["COIRE_RUN_ID"])
    profile = ProfileName(env["COIRE_PROFILE"])
    model_id = uuid.UUID(env["COIRE_MODEL_ID"])
    verified_variant_id = uuid.UUID(env["COIRE_VERIFIED_VARIANT_ID"])
    request = load_request(request_path)
    if request.profile is not profile:
        raise ValueError("workspace profile differs from admitted run")
    if request.variant_id != verified_variant_id:
        raise ValueError("workspace variant is not the admitted verified variant")

    transport = GatewayTransport(
        gateway_url=env["COIRE_API_URL"],
        token=env["COIRE_RUN_TOKEN"],
        model_id=str(model_id),
    )

    async def verified(candidate: uuid.UUID) -> bool:
        return candidate == verified_variant_id

    async def repair(invalid: str, error: str) -> str:
        return await transport.complete_repair(invalid=invalid, error=error)

    harness = Harness(
        transport,
        repair=repair,
        verify_variant=verified,
        retry_limit=int(env.get("COIRE_HARNESS_RETRY_LIMIT", "2")),
        tool_byte_cap=int(env.get("COIRE_HARNESS_TOOL_OUTPUT_BYTE_CAP", "16384")),
    )
    result = await harness.run_structured(request, OUTPUT_TYPES[profile])
    result.run_id = run_id
    result_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = result_path.with_suffix(".json.tmp")
    temporary.write_text(result.model_dump_json(), encoding="utf-8")
    temporary.replace(result_path)


def main() -> None:
    asyncio.run(execute())


if __name__ == "__main__":
    main()
