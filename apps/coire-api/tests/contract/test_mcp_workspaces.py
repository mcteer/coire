"""Registered workspace ownership and source validation contracts."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind
from coire_api.routes.workspaces import _browser_write_origin
from coire_api.workspaces import WorkspaceSourceError, resolve_source, validate_repository_url
from coire_core.models.mcp import WorkspaceRegistrationCreate, WorkspaceSource
from coire_core.settings import Settings


def _settings() -> Settings:
    return Settings(_secrets_dir="/nonexistent", mcp_source_hosts="github.com")  # type: ignore[call-arg]


def test_registration_routes_are_authenticated_and_strict() -> None:
    document = create_app(_settings()).openapi()
    for path, methods in {
        "/api/v1/workspaces": ("post", "get"),
        "/api/v1/workspaces/{workspace_id}": ("delete",),
    }.items():
        for method in methods:
            assert document["paths"][path][method]["security"] == [{"HTTPBearer": []}]
    assert (
        document["components"]["schemas"]["WorkspaceRegistrationCreate"]["additionalProperties"]
        is False
    )


def test_browser_workspace_writes_require_exact_origin_but_api_keys_remain_usable() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(settings=SimpleNamespace(chat_browser_origin="https://coire.test"))
    )

    def request(origin: str | None) -> Request:
        headers = [] if origin is None else [(b"origin", origin.encode())]
        return Request({"type": "http", "method": "POST", "headers": headers, "app": app})

    browser = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    key = Principal(kind=PrincipalKind.API_KEY, user_id=uuid.uuid4())
    _browser_write_origin(request("https://coire.test"), browser)
    _browser_write_origin(request(None), key)
    for origin in (None, "https://other.test"):
        with pytest.raises(HTTPException) as refused:
            _browser_write_origin(request(origin), browser)
        assert refused.value.status_code == 403


def test_source_url_is_allowlisted_and_registration_rejects_credentials() -> None:
    valid = WorkspaceSource(repository_url="https://github.com/org/repo.git", revision="main")  # type: ignore[arg-type]
    validate_repository_url(valid, _settings())
    forbidden = WorkspaceSource(repository_url="https://127.0.0.1/repo.git", revision="main")  # type: ignore[arg-type]
    with pytest.raises(WorkspaceSourceError, match="not allowed"):
        validate_repository_url(forbidden, _settings())
    with pytest.raises(ValidationError):
        WorkspaceRegistrationCreate(repository_url="https://user:secret@github.com/repo.git")  # type: ignore[arg-type]


async def test_unknown_registered_workspace_is_refused_without_disclosing_ownership() -> None:
    class EmptySession:
        async def scalar(self, query: object) -> None:
            return None

    source = WorkspaceSource(workspace_id=uuid.uuid4(), revision="main")
    with pytest.raises(WorkspaceSourceError, match="not available"):
        await resolve_source(
            EmptySession(),  # type: ignore[arg-type]
            owner_user_id=uuid.uuid4(),
            source=source,
            settings=_settings(),
        )
