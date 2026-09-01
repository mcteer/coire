from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from coire_core.models import LinkObservation, LinkProbeCommand
from coire_node.link_probes import LinkProbeRunner

router = APIRouter(prefix="/node/link-probes", tags=["link probes"])


@router.post("", response_model=LinkObservation)
async def run_probe(command: LinkProbeCommand, request: Request) -> LinkObservation:
    runner = request.app.state.link_probes
    if not isinstance(runner, LinkProbeRunner):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "link probes unavailable")
    try:
        return runner.run(command)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "generated hostfile missing") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
