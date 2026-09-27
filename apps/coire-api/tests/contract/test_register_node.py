"""Version-matched registration contracts for feature 022."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from coire_api.auth import ANONYMOUS
from coire_api.instance.registration import token_digest
from coire_api.routes.nodes import load_inventory, register_node
from coire_core.models.node import (
    Node,
    NodeRegistration,
    NodeRegistrationV2,
    NodeRole,
    NodeV2,
    Reachability,
)
from coire_core.settings import Settings


class Result:
    def __init__(self, row: Any) -> None:
        self.row = row

    def scalar_one_or_none(self) -> Any:
        return self.row


class Session:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.row: Any = SimpleNamespace(
            id=uuid.uuid4(),
            name="coire-edge-a",
            role=NodeRole.STUDIO,
            mesh_address="192.168.100.11",
            egress_address=None,
            endpoint_contract_version=None,
            control_host=None,
            data_host=None,
            memory_total_bytes=1,
            disk_total_bytes=1,
            gpu_cores=None,
            agent_version="0.1.0",
            registered_at=now,
            last_seen_at=now,
            reachability=Reachability.UNKNOWN,
            probe_failures=0,
            declared_at=now,
            registration_token_digest=token_digest(
                "token",
                Settings(  # type: ignore[call-arg]
                    _secrets_dir="/nonexistent", key_signing_secret=SecretStr("contract-secret")
                ),
            ),
            token_issued_at=now,
            token_consumed_at=None,
            token_revoked_at=None,
        )
        self.added: list[Any] = []

    async def execute(self, *_: Any) -> Result:
        return Result(self.row)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def refresh(self, row: Any) -> None:
        return None


def settings(tmp_path: Path) -> Settings:
    inventory = tmp_path / "nodes.yaml"
    inventory.write_text(
        "nodes:\n  coire-edge-a:\n    role: studio\n"
        "    control_host: coire-edge-a.lab\n    data_host: coire-edge-a.fabric\n"
    )
    result = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", key_signing_secret=SecretStr("contract-secret")
    )
    result.node_inventory_file = str(inventory)
    result.node_tokens = type(result.node_tokens)('{"coire-edge-a":"token"}')
    load_inventory.cache_clear()
    return result


def request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "app": type("App", (), {"state": type("State", (), {})()})(),
        }
    )


async def test_v1_request_receives_v1_response(tmp_path: Path) -> None:
    response = await register_node(
        NodeRegistration.model_validate(
            {
                "name": "coire-edge-a",
                "token": "token",
                "mesh_address": "192.168.100.11",
                "memory_total_bytes": 1,
                "disk_total_bytes": 1,
                "agent_version": "0.1.0",
            }
        ),
        request(),
        ANONYMOUS,
        cast(AsyncSession, Session()),
        settings(tmp_path),
    )
    assert isinstance(response, Node)
    assert not hasattr(response, "endpoints")


async def test_v2_request_receives_v2_response(tmp_path: Path) -> None:
    response = await register_node(
        NodeRegistrationV2.model_validate(
            {
                "name": "coire-edge-a",
                "token": "token",
                "endpoints": {
                    "contract_version": 2,
                    "control_host": "coire-edge-a.lab",
                    "data_host": "coire-edge-a.fabric",
                },
                "memory_total_bytes": 1,
                "disk_total_bytes": 1,
                "agent_version": "0.2.0",
            }
        ),
        request(),
        ANONYMOUS,
        cast(AsyncSession, Session()),
        settings(tmp_path),
    )
    assert isinstance(response, NodeV2)
    assert response.endpoints.control_host == "coire-edge-a.lab"


async def test_bare_v2_control_name_is_accepted_during_rollout(tmp_path: Path) -> None:
    response = await register_node(
        NodeRegistrationV2.model_validate(
            {
                "name": "coire-edge-a",
                "token": "token",
                "endpoints": {
                    "contract_version": 2,
                    "control_host": "coire-edge-a",
                    "data_host": "coire-edge-a.fabric",
                },
                "memory_total_bytes": 1,
                "disk_total_bytes": 1,
                "agent_version": "0.2.0",
            }
        ),
        request(),
        ANONYMOUS,
        cast(AsyncSession, Session()),
        settings(tmp_path),
    )
    assert isinstance(response, NodeV2)
    assert response.endpoints.control_host == "coire-edge-a"


async def test_v2_endpoint_must_match_inventory(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        await register_node(
            NodeRegistrationV2.model_validate(
                {
                    "name": "coire-edge-a",
                    "token": "token",
                    "endpoints": {
                        "contract_version": 2,
                        "control_host": "coire-edge-a.lab",
                        "data_host": "coire-edge-b.fabric",
                    },
                    "memory_total_bytes": 1,
                    "disk_total_bytes": 1,
                    "agent_version": "0.2.0",
                }
            ),
            request(),
            ANONYMOUS,
            cast(AsyncSession, Session()),
            settings(tmp_path),
        )
    assert exc.value.status_code == 403
