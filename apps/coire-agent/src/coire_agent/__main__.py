"""Strict workspace-request entrypoint for the ephemeral Studio harness image."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path

from coire_agent.coding import CodingWorkspace, run_coding
from coire_agent.evaluation import execute_phase
from coire_agent.evaluation_inputs import read_previous_outputs
from coire_agent.gateway_model import GatewayTransport
from coire_agent.harness import Harness
from coire_agent.pydantic_runtime import OUTPUT_TYPES
from coire_core.models.adapters import InferenceTarget
from coire_core.models.evaluation import EvaluationRuntime, EvaluationWorkload
from coire_core.models.harness import HarnessRunRequest, ProfileName

REQUEST_PATH = Path("/workspace/.coire/request.json")
RESULT_PATH = Path("/workspace/.coire/result.json")
SEPARATE_RESULT_PATH = Path("/coire-output/result.json")
MAX_REQUEST_BYTES = 2 * 1024**2


def load_request(path: Path = REQUEST_PATH) -> HarnessRunRequest | EvaluationWorkload:
    size = path.stat().st_size
    if size <= 0 or size > MAX_REQUEST_BYTES:
        raise ValueError("harness request size is invalid")
    data = json.loads(path.read_bytes())
    if isinstance(data, dict) and data.get("kind") == "evaluation":
        return EvaluationWorkload.model_validate(data)
    return HarnessRunRequest.model_validate(data)


async def execute(
    *,
    environ: Mapping[str, str] | None = None,
    request_path: Path = REQUEST_PATH,
    result_path: Path | None = None,
) -> None:
    env = environ or os.environ
    if result_path is None:
        result_path = SEPARATE_RESULT_PATH if env.get("COIRE_OUTPUT_DIR") else RESULT_PATH
    run_id = uuid.UUID(env["COIRE_RUN_ID"])
    profile = ProfileName(env["COIRE_PROFILE"])
    model_id = uuid.UUID(env["COIRE_MODEL_ID"])
    verified_variant_id = uuid.UUID(env["COIRE_VERIFIED_VARIANT_ID"])
    request = load_request(request_path)
    target = (
        InferenceTarget.model_validate_json(env["COIRE_INFERENCE_TARGET"])
        if env.get("COIRE_INFERENCE_TARGET")
        else None
    )
    if isinstance(request, EvaluationWorkload):
        if (
            request.run_id != run_id
            or request.target.target != target
            or request.target.target.model_id != model_id
            or request.target.target.variant_id != verified_variant_id
            or env.get("COIRE_OUTPUT_DIR") != str(result_path.parent)
            or env.get("COIRE_RUN_PURPOSE") != "evaluation"
        ):
            raise ValueError("evaluation workload differs from admitted run")
        actual_runtime = EvaluationRuntime.model_validate_json(env["COIRE_EVALUATION_RUNTIME"])
        transport = GatewayTransport(
            gateway_url=env["COIRE_API_URL"],
            token=env["COIRE_RUN_TOKEN"],
            model_id=env.get("COIRE_PUBLIC_SELECTOR", str(model_id)),
            target=target,
            variant_id=verified_variant_id,
        )
        prior_outputs = (
            await asyncio.to_thread(read_previous_outputs, request, request_path.parent / "inputs")
            if request.phase == "judge" and request.input_files
            else None
        )
        if request.pressure is not None:
            from coire_agent.evaluation_pressure import execute_pressure

            evaluation_result = await execute_pressure(
                request,
                transport.complete_evaluation,
                runtime=actual_runtime,
                stop_path=request_path.parent / "measurement-stop.json",
                prior_outputs=prior_outputs,
                input_root=request_path.parent / "inputs",
            )
        else:
            evaluation_result = await execute_phase(
                request,
                transport.complete_evaluation,
                runtime=actual_runtime,
                prior_outputs=prior_outputs,
                input_root=request_path.parent / "inputs",
            )
        result_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = result_path.with_suffix(".json.tmp")
        temporary.write_text(evaluation_result.model_dump_json(), encoding="utf-8")
        temporary.replace(result_path)
        return
    if request.target is None and target is not None and target.adapter_id is None:
        # UUID-only workspace requests remain compatible with newly frozen base grants.
        request = request.model_copy(update={"target": target})
    if request.target != target or (
        target is not None
        and (target.model_id != model_id or target.variant_id != verified_variant_id)
    ):
        raise ValueError("workspace target differs from admitted exact target")
    if request.profile is not profile:
        raise ValueError("workspace profile differs from admitted run")
    if request.variant_id != verified_variant_id:
        raise ValueError("workspace variant is not the admitted verified variant")
    if request.coding_mode is not None and env.get("COIRE_OUTPUT_DIR") != str(result_path.parent):
        raise ValueError("MCP coding run requires a separate output mount")
    if request.coding_mode is not None and "COIRE_HARNESS_VERIFIED" not in env:
        raise ValueError("MCP coding run requires explicit variant verification state")

    transport = GatewayTransport(
        gateway_url=env["COIRE_API_URL"],
        token=env["COIRE_RUN_TOKEN"],
        model_id=env.get("COIRE_PUBLIC_SELECTOR", str(model_id)),
        target=target,
        variant_id=verified_variant_id if target else None,
    )

    async def verified(candidate: uuid.UUID) -> bool:
        return (
            candidate == verified_variant_id and env.get("COIRE_HARNESS_VERIFIED", "true") == "true"
        )

    async def verified_target(candidate: InferenceTarget) -> bool:
        return candidate == target and env.get("COIRE_HARNESS_VERIFIED", "false") == "true"

    async def repair(invalid: str, error: str) -> str:
        return await transport.complete_repair(invalid=invalid, error=error)

    harness = Harness(
        transport,
        repair=repair,
        verify_variant=verified,
        verify_target=verified_target,
        retry_limit=int(env.get("COIRE_HARNESS_RETRY_LIMIT", "2")),
        tool_byte_cap=int(env.get("COIRE_HARNESS_TOOL_OUTPUT_BYTE_CAP", "16384")),
    )
    if request.coding_mode is None:
        result = await harness.run_structured(request, OUTPUT_TYPES[profile])
    else:
        result = await run_coding(
            request,
            harness,
            workspace=CodingWorkspace(request_path.parent.parent, result_path.parent),
            run_id=run_id,
        )
    result.run_id = run_id
    result_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = result_path.with_suffix(".json.tmp")
    temporary.write_text(result.model_dump_json(), encoding="utf-8")
    temporary.replace(result_path)


def main() -> None:
    asyncio.run(execute())


if __name__ == "__main__":
    main()
