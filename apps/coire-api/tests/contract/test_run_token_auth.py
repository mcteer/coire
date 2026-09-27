from __future__ import annotations

import uuid
from typing import cast

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.gateway.resolution import ModelNotFoundError, resolve_model
from coire_api.routes.v1 import _enforce_run_request_scope


def principal(model_id: uuid.UUID) -> Principal:
    return Principal(
        kind=PrincipalKind.RUN,
        subject="run",
        user_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        scopes=frozenset({"chat"}),
        permitted_model_ids=frozenset({model_id}),
        permitted_tools=frozenset({"read_file"}),
        spend_limit_tokens=100,
        spent_tokens=10,
    )


def test_run_scope_bounds_tools_and_projected_spend() -> None:
    model_id = uuid.uuid4()
    run = principal(model_id)
    assert (
        _enforce_run_request_scope(
            run,
            tools=[{"type": "function", "function": {"name": "read_file"}}],
            prompt_tokens=20,
            max_tokens=None,
        )
        == 70
    )
    with pytest.raises(HTTPException, match="tool"):
        _enforce_run_request_scope(
            run,
            tools=[{"type": "function", "function": {"name": "shell"}}],
            prompt_tokens=20,
            max_tokens=1,
        )
    with pytest.raises(HTTPException, match="spend"):
        _enforce_run_request_scope(run, tools=None, prompt_tokens=20, max_tokens=71)


class MustNotReadSession:
    async def get(self, *_: object) -> None:
        raise AssertionError("out-of-scope model must stop before a registry read")


async def test_out_of_scope_model_is_uniformly_not_found() -> None:
    with pytest.raises(ModelNotFoundError):
        await resolve_model(
            cast(AsyncSession, MustNotReadSession()),
            uuid.uuid4(),
            principal(uuid.uuid4()),
        )
