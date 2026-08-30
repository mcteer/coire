"""Node routes for repositories and local copies."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from coire_core.models.jobs import JobErrorKind, RepoInspection
from coire_core.models.registry import SLUG_PATTERN
from coire_node import hub
from coire_node.deps import EngineDep, GrantsDep, SettingsDep, StoreDep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/node/models", tags=["models"])


class InspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    revision: str = "main"


class ExportGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant: str = Field(min_length=32)
    expires_at: datetime


class LocalCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    path: str
    bytes: int
    manifest_present: bool
    manifest_sha256: str | None = None
    verified_at: datetime | None = None
    serving_engine_ids: list[str] = Field(default_factory=list)


_ERROR_STATUS = {
    JobErrorKind.GATED: status.HTTP_423_LOCKED,
    JobErrorKind.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    JobErrorKind.NETWORK: status.HTTP_502_BAD_GATEWAY,
}


@router.post("/inspect", response_model=RepoInspection)
async def inspect_repo(request: InspectRequest, settings: SettingsDep) -> RepoInspection:
    """Read repository metadata. The only component that talks to Hugging Face (FR-005)."""
    try:
        return hub.inspect(
            request.repo_id,
            revision=request.revision,
            token=settings.hf_token.get_secret_value(),
            cache_dir=settings.node_hf_cache_dir,
        )
    except hub.HubError as exc:
        raise HTTPException(
            status_code=_ERROR_STATUS.get(exc.kind, status.HTTP_502_BAD_GATEWAY),
            detail=str(exc),
        ) from exc


@router.get("", response_model=list[LocalCopy])
async def list_local(store: StoreDep, engines: EngineDep) -> list[LocalCopy]:
    copies: list[LocalCopy] = []
    for path in sorted(p for p in store.root.iterdir() if p.is_dir()):
        slug = path.name
        manifest = store.read_manifest(slug)
        copies.append(
            LocalCopy(
                slug=slug,
                path=str(path),
                bytes=store.size_bytes(slug),
                manifest_present=manifest is not None,
                manifest_sha256=manifest.sha256() if manifest else None,
                verified_at=manifest.created_at if manifest else None,
                serving_engine_ids=[
                    str(e.engine_id)
                    for e in engines.statuses()
                    if e.slug == slug and e.engine_id is not None
                ],
            )
        )
    return copies


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_local(slug: str, store: StoreDep, engines: EngineDep) -> Response:
    """Remove a copy. Refused while an engine serves it — deleting the weights out from under
    a running process produces a confusing failure much later."""
    if not _valid(slug):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a model slug")
    serving = [e for e in engines.statuses() if e.slug == slug and e.state.value != "stopped"]
    if serving:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{slug} is being served by engine(s) {[str(e.engine_id) for e in serving]}; "
            "unload first",
        )
    store.delete(slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{slug}/export", status_code=status.HTTP_204_NO_CONTENT)
async def grant_export(
    slug: str, request: ExportGrantRequest, store: StoreDep, grants: GrantsDep
) -> Response:
    """Authorise one peer to fetch this copy over the mesh."""
    if not _valid(slug):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a model slug")
    if not store.exists(slug) or store.read_manifest(slug) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no verified copy of {slug} here")
    grants.register(request.grant, slug, request.expires_at)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{slug}/export", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_export(slug: str, grants: GrantsDep) -> Response:
    grants.revoke_for(slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _valid(slug: str) -> bool:
    import re

    return bool(re.match(SLUG_PATTERN, slug))
