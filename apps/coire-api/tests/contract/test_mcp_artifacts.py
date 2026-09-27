"""Branch bundle metadata is owner scoped and expires before download."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import McpArtifactRow
from coire_api.routes.mcp_artifacts import _owned_artifact
from coire_core.settings import Settings


class Session:
    def __init__(self, row: McpArtifactRow) -> None:
        self.row = row

    async def get(self, _type: object, artifact_id: uuid.UUID) -> McpArtifactRow | None:
        return self.row if self.row.id == artifact_id else None


async def test_artifact_owner_and_expiry_gate() -> None:
    owner = uuid.uuid4()
    row = McpArtifactRow(
        id=uuid.uuid4(), owner_user_id=owner, run_id=uuid.uuid4(),
        call_id=uuid.uuid4(), storage_ref="edge-a", sha256="a" * 64,
        size_bytes=512, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = Session(row)
    principal = Principal(kind=PrincipalKind.USER, user_id=owner)
    assert await _owned_artifact(row.id, principal, session) is row  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as other_user:
        await _owned_artifact(
            row.id, Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
            session,  # type: ignore[arg-type]
        )
    assert other_user.value.status_code == 404
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as expired:
        await _owned_artifact(row.id, principal, session)  # type: ignore[arg-type]
    assert expired.value.status_code == 404


def test_artifact_routes_are_authenticated_in_openapi() -> None:
    app = create_app(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/mcp/artifacts/{artifact_id}",
        "/api/v1/mcp/artifacts/{artifact_id}/metadata",
    ):
        assert paths[path]["get"]["security"] == [{"HTTPBearer": []}]
