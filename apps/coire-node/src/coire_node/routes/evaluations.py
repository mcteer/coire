"""Authenticated evaluation control routes; no caller-selected filesystem paths."""

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request

from coire_core.models.evaluation import (
    EvaluationCapabilities,
    EvaluationIdentityRequest,
    EvaluationInputReceipt,
    EvaluationPressureStop,
    EvaluationProbePrepare,
    EvaluationProbePrepared,
    EvaluationRuntime,
    EvaluationTrainingInputsReceipt,
    EvaluationWorkspaceCleanup,
    EvaluationWorkspacePrepare,
    EvaluationWorkspaceReceipt,
)
from coire_core.models.evaluation_inputs import EvaluationTrainingInputsRequest
from coire_node.routes.workspaces import _manager, _translate
from coire_node.workspaces import WorkspaceError

router = APIRouter(prefix="/node/evaluations", tags=["evaluations"])


@router.post("/probes", response_model=EvaluationProbePrepared)
async def prepare_probes(body: EvaluationProbePrepare, request: Request) -> EvaluationProbePrepared:
    from coire_core.evaluation_suites.measurement import prompt_digest
    from coire_node.evaluation_tokenizer_process import inspect_tokenizer

    if body.prompt_set_sha256 != prompt_digest() or len(
        {target.instance_id for target in body.targets}
    ) != len(body.targets):
        raise HTTPException(422, "probe set differs from installed protocol")
    manager = _manager(request)
    for target in body.targets:
        await identity(target.identity, request)
    try:
        raw = await inspect_tokenizer(
            "probes",
            body.model_dump_json().encode(),
            store_root=manager.settings.node_store_dir,
            agent_image=manager.settings.run_agent_image,
        )
        return EvaluationProbePrepared.model_validate_json(raw)
    except Exception:
        raise HTTPException(409, "probe token counts are unavailable") from None


@router.post("/workspaces/{run_id}/pressure-stop", status_code=204)
async def stop_pressure(run_id: uuid.UUID, body: EvaluationPressureStop, request: Request) -> None:
    if body.run_id != run_id:
        raise HTTPException(422, "run ID differs")
    try:
        await _manager(request).evaluations.stop_pressure(body)
    except WorkspaceError as error:
        raise _translate(error) from error


@router.put("/workspaces/{run_id}/inputs/{name}", response_model=EvaluationInputReceipt)
async def stage_input(run_id: uuid.UUID, name: str, request: Request) -> EvaluationInputReceipt:
    digest = request.headers.get("x-coire-request-sha256", "")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 256 * 1024**2:
            raise HTTPException(413, "evaluation input exceeds bound")
    try:
        return await _manager(request).evaluations.stage_input(run_id, digest, name, bytes(payload))
    except WorkspaceError as error:
        raise _translate(error) from error


@router.post("/workspaces", response_model=EvaluationWorkspaceReceipt, status_code=201)
async def prepare(body: EvaluationWorkspacePrepare, request: Request) -> EvaluationWorkspaceReceipt:
    if body.workload.target.variant_slug is None:
        raise HTTPException(422, "evaluation requires registry variant identity")
    runtime = await identity(
        EvaluationIdentityRequest(
            engine_backend=body.workload.target.engine_backend,
            target=body.workload.target.target,
            variant_slug=body.workload.target.variant_slug,
            template_override=body.workload.target.template_override,
            capability_profile=body.workload.target.capability_profile,
        ),
        request,
    )
    if runtime != body.workload.target.runtime:
        raise HTTPException(409, "evaluation runtime differs from admitted identity")
    try:
        return await _manager(request).evaluations.prepare(body)
    except WorkspaceError as error:
        raise _translate(error) from error


@router.post("/workspaces/{run_id}/cleanup", status_code=204)
async def cleanup(run_id: uuid.UUID, body: EvaluationWorkspaceCleanup, request: Request) -> None:
    if body.run_id != run_id:
        raise HTTPException(422, "run ID differs")
    manager = _manager(request)
    container = await manager.docker.inspect_container(manager.container_name(run_id))
    if container is not None and bool((container.get("State") or {}).get("Running")):
        raise HTTPException(409, "evaluation container remains active")
    try:
        await manager.evaluations.cleanup(run_id, body.request_sha256)
    except WorkspaceError as error:
        raise _translate(error) from error


@router.get("/capabilities", response_model=EvaluationCapabilities)
async def capabilities(request: Request) -> EvaluationCapabilities:
    manager = _manager(request)
    image = (
        await manager.docker.inspect_image(manager.settings.run_agent_image)
        if manager.settings.run_agent_image
        else None
    )
    labels = (image.get("Config") or {}).get("Labels") or {} if image else {}
    supported = (
        labels.get("com.coire.evaluation.workload") == "1"
        and labels.get("com.coire.harness.version") == "0.1.0"
    )
    return EvaluationCapabilities.model_validate(
        {
            "node": manager.settings.node_name,
            "workload_versions": [1] if supported else [],
            "agent_image": manager.settings.run_agent_image if supported else None,
            "harness_version": labels.get("com.coire.harness.version") if supported else None,
        }
    )


@router.post("/identity", response_model=EvaluationRuntime)
async def identity(body: EvaluationIdentityRequest, request: Request) -> EvaluationRuntime:
    from coire_node.evaluation_tokenizer_process import inspect_tokenizer

    manager = _manager(request)
    capability = await capabilities(request)
    if not capability.workload_versions or capability.harness_version is None:
        raise HTTPException(409, "evaluation workload is unsupported by installed agent image")
    from coire_node.evaluation_tokenizer_worker import engine_runtime_version

    engine_version = engine_runtime_version(body.engine_backend)
    if body.engine_id is not None:
        from coire_node.deps import get_engines

        observed = (await get_engines(request)).attested_engine_version(
            body.engine_id, body.target, body.template_override, backend=body.engine_backend
        )
        if observed is None or observed != engine_version:
            raise HTTPException(409, "owned engine runtime cannot be attested")
    try:
        raw = await inspect_tokenizer(
            "identity",
            body.model_dump_json().encode(),
            store_root=manager.settings.node_store_dir,
            agent_image=manager.settings.run_agent_image,
        )
        result = EvaluationRuntime.model_validate_json(raw)
        if (
            result.engine_version != engine_version
            or result.harness_version != capability.harness_version
        ):
            raise ValueError("inspection runtime differs from installed capability")
        return result
    except Exception:
        raise HTTPException(409, "evaluation runtime identity is unavailable") from None


@router.post("/workspaces/{run_id}/training-inputs", response_model=EvaluationTrainingInputsReceipt)
async def stage_training_inputs(
    run_id: uuid.UUID, body: EvaluationTrainingInputsRequest, request: Request
) -> EvaluationTrainingInputsReceipt:
    """Stage only registered granted sources and the local full-checkpoint state."""
    from datetime import UTC, datetime

    import httpx

    manager = _manager(request)
    if body.run_id != run_id:
        raise HTTPException(422, "run ID differs")
    try:
        workload = await asyncio.to_thread(
            manager.evaluations.workload, run_id, body.request_sha256
        )
        binding = workload.training
        if binding is None or len(body.grants) != len(binding.sources):
            raise ValueError("training source grants differ")
        by_id = {grant.dataset_id: grant for grant in body.grants}
        if len(by_id) != len(body.grants) or set(by_id) != {
            source.dataset_id for source in binding.sources
        }:
            raise ValueError("training source identities differ")
        artifacts = request.app.state.training_artifacts
        if artifacts is None:
            raise ValueError("checkpoint store is unavailable")
        manifest = await asyncio.to_thread(artifacts.manifest, binding.checkpoint_id)
        if manifest.canonical_sha256() != binding.checkpoint_manifest_sha256:
            raise ValueError("checkpoint identity differs")
        entry = next((file for file in manifest.files if file.id == binding.state_file_id), None)
        if (
            entry is None
            or entry.sha256 != binding.state_sha256
            or entry.bytes != binding.state_bytes
        ):
            raise ValueError("checkpoint state identity differs")
        path = await asyncio.to_thread(artifacts.file, manifest, entry.id)
        raw = await asyncio.to_thread(manager.evaluations._read, path, binding.state_bytes)
        receipts = [
            await manager.evaluations.stage_input(
                run_id, body.request_sha256, "training-state.json", raw
            )
        ]
        for source in binding.sources:
            grant = by_id[source.dataset_id]
            if (
                grant.evaluation_attempt_id != workload.attempt_id
                or grant.node != manager.settings.node_name
                or grant.source_sha256 != source.source_sha256
                or grant.max_bytes != source.source_bytes
                or grant.expires_at <= datetime.now(UTC)
            ):
                raise ValueError("training input grant differs from this Studio phase")
            data = bytearray()
            timeout = max(
                0,
                min(
                    30,
                    (min(grant.expires_at, workload.deadline) - datetime.now(UTC)).total_seconds(),
                ),
            )
            async with (
                asyncio.timeout(timeout),
                httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client,
                client.stream(
                    "GET",
                    f"{manager.settings.training_input_api_url.rstrip('/')}/api/v1/internal/training/datasets/{source.dataset_id}/content",
                    headers={
                        "Authorization": "Bearer " + manager.settings.node_token.get_secret_value(),
                        "X-Coire-Node": grant.node,
                        "X-Coire-Dataset-Grant": grant.secret,
                    },
                ) as response,
            ):
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("compressed input is unsupported")
                async for chunk in response.aiter_bytes(64 * 1024):
                    data.extend(chunk)
                    if len(data) > grant.max_bytes or grant.expires_at <= datetime.now(UTC):
                        raise ValueError("input delivery exceeds grant")
            receipts.append(
                await manager.evaluations.stage_input(
                    run_id, body.request_sha256, f"source-{source.dataset_id}.jsonl", bytes(data)
                )
            )
        return EvaluationTrainingInputsReceipt(items=receipts)
    except (WorkspaceError, OSError, ValueError, TimeoutError, httpx.HTTPError):
        raise HTTPException(409, "Immutable training evaluation inputs are unavailable") from None
