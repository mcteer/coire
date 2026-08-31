"""The peer replication data path.

Mounted **only on the Studio data listener** (see `agent.create_app`): a model copy may not
cross the control interface, and the cheapest way to guarantee that is for the route not to
exist there at all.

These are the only unauthenticated routes on the agent. They are authorised by the grant in
the path — one model, one node, expiring — rather than by the node's bearer token, because the
peer does not have that token and should not.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse

from coire_node.deps import GrantsDep, StoreDep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/node/export", tags=["export"])

_GRANT_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _resolve(grants, grant: str) -> str:  # type: ignore[no-untyped-def]
    """The slug a grant authorises.

    Unknown, expired and revoked are all 404 and deliberately indistinguishable: telling a
    caller which one it was would confirm that a grant existed.
    """
    if not _GRANT_RE.match(grant):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such export")
    slug = grants.resolve(grant)
    if slug is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such export")
    return str(slug)


@router.get("/{grant}/manifest")
async def export_manifest(grant: str, grants: GrantsDep, store: StoreDep) -> JSONResponse:
    slug = _resolve(grants, grant)
    manifest = store.read_manifest(slug)
    if manifest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such export")
    return JSONResponse(manifest.model_dump(mode="json"))


@router.get("/{grant}/files/{path:path}")
async def export_file(
    grant: str, path: str, request: Request, grants: GrantsDep, store: StoreDep
) -> FileResponse:
    """Stream one file of the granted copy.

    `FileResponse` handles Range itself, which is what lets an interrupted import resume from
    the byte it reached instead of starting the file again.
    """
    slug = _resolve(grants, grant)
    base = store.path_for(slug)
    target = (base / path).resolve()
    # The grant scopes *which model*; this scopes *which file within it*. A manifest arrives
    # from another node, so its paths are not trusted even though ManifestFile validates them.
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such file")
    return FileResponse(Path(target), media_type="application/octet-stream")
