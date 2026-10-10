"""Only a failed readonly lookup may reconnect; admission is never replayed."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.routes import internal_training
from coire_api.training.service import payload_digest
from coire_core.models.adapters import InferenceTarget
from coire_core.models.auth import UserRole
from coire_core.models.training import (
    TrainingMeasurementCompletion,
    TrainingMeasurementGenerateRequest,
    TrainingMeasurementPrompt,
    TrainingResidentTarget,
)
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "failure", ["lookup", "second_lookup", "statement", "generation", "commit"]
)
async def test_reconnect_only_before_admission(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    owner = uuid.uuid4()
    principal = Principal(kind=PrincipalKind.USER, user_id=owner, role=UserRole.ADMIN)
    target = TrainingResidentTarget(
        instance_id=uuid.uuid4(),
        target=InferenceTarget(
            model_id=uuid.uuid4(), variant_id=uuid.uuid4(), base_manifest_sha256="a" * 64
        ),
    )
    body = TrainingMeasurementGenerateRequest(
        principal_sha256=payload_digest(principal),
        target=target,
        prompt=TrainingMeasurementPrompt(text="synthetic input", input_tokens=4000),
        max_output_tokens=1,
    )
    scopes: list[int] = []
    generations: list[int] = []
    rollbacks: list[int] = []

    class Result:
        def one_or_none(self) -> tuple[object, ...]:
            return ("running", owner, ["coire-edge-a"], principal.model_dump(mode="json"))

    class Session:
        async def rollback(self) -> None:
            rollbacks.append(1)

        async def execute(self, *args: object) -> Result:
            if (failure == "lookup" and len(scopes) == 1) or failure in {
                "second_lookup",
                "statement",
            }:
                raise DBAPIError(
                    None,
                    None,
                    RuntimeError("synthetic disconnect"),
                    connection_invalidated=failure != "statement",
                )
            return Result()

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        scopes.append(1)
        yield cast(AsyncSession, Session())
        if failure == "commit":
            raise DBAPIError(
                None, None, RuntimeError("synthetic disconnect"), connection_invalidated=True
            )

    async def generate(*args: object) -> TrainingMeasurementCompletion:
        generations.append(1)
        if failure == "generation":
            raise DBAPIError(
                None, None, RuntimeError("synthetic disconnect"), connection_invalidated=True
            )
        return TrainingMeasurementCompletion(
            instance_id=target.instance_id,
            target=target.target,
            first_token_seconds=0.1,
            input_tokens=4000,
            output_tokens=1,
        )

    monkeypatch.setattr(internal_training, "session_scope", scope)
    settings = Settings(
        training_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-node-token"}')
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            training_measurement_gateway=generate,
            training_measurement_database=SimpleNamespace(session=scope),
        )
    )
    request = Request({"type": "http", "app": app})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="fixture-node-token")
    if failure != "lookup":
        with pytest.raises(DBAPIError):
            await internal_training.generate_measurement(
                uuid.uuid4(), body, request, credentials, "coire-edge-a"
            )
        assert len(scopes) == (2 if failure == "second_lookup" else 1)
        assert len(generations) == (1 if failure in {"generation", "commit"} else 0)
    else:
        result = await internal_training.generate_measurement(
            uuid.uuid4(), body, request, credentials, "coire-edge-a"
        )
        assert result.input_tokens == 4000
        assert len(scopes) == 2 and len(generations) == 1
        assert len(rollbacks) == 1
