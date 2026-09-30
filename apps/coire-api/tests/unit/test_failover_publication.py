"""Core publishes only authorised registry rows in a pinned, signed snapshot."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from coire_api.failover.publication import CoreSnapshotService, configured_membership
from coire_core.failover_snapshot import load_verified_snapshot
from coire_core.settings import Settings


def _key() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    return private, public


@pytest.mark.asyncio
async def test_publisher_signs_a_fresh_registry_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, core_public = _key()
    _, edge_a = _key()
    _, edge_b = _key()
    path = tmp_path / "snapshot.json"
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_member_name="coire-core",
        failover_peer_key=SecretStr(private),
        failover_core_public_key=core_public,
        failover_edge_a_public_key=edge_a,
        failover_edge_b_public_key=edge_b,
        failover_snapshot_path=str(path),
        cloudflare_access_issuer="https://access.example.test",
        cloudflare_access_audience="test-audience",
    )
    model_id = uuid4()

    class _Session:
        async def scalars(self, _query: object) -> SimpleNamespace:
            assert "models.kind" in str(_query)
            row = SimpleNamespace(
                id=model_id,
                slug="tiny-model",
                display_name="Tiny Model",
                entitlement=[],
                context_window=2048,
                source="studio",
                backend="mlx_lm",
                kind="language_model",
            )
            visual = SimpleNamespace(**{**vars(row), "id": uuid4(), "backend": "mlx_vlm"})
            provider = SimpleNamespace(**{**vars(row), "id": uuid4(), "source": "openai"})
            image = SimpleNamespace(
                **{**vars(row), "id": uuid4(), "backend": "mflux", "kind": "image_model"}
            )
            auxiliary = SimpleNamespace(
                **{**vars(row), "id": uuid4(), "backend": "auxiliary", "kind": "image_lora"}
            )
            malformed = SimpleNamespace(**{**vars(row), "id": uuid4(), "kind": "image_model"})
            return SimpleNamespace(all=lambda: [row, visual, provider, image, auxiliary, malformed])

    @asynccontextmanager
    async def _session() -> AsyncIterator[_Session]:
        yield _Session()

    monkeypatch.setattr("coire_api.failover.publication.session_scope", _session)
    membership = configured_membership(settings)
    assert membership is not None
    published = await CoreSnapshotService(settings, membership).publish_once(datetime.now(UTC))
    verified = load_verified_snapshot(path, core_public)
    assert verified.snapshot_id == published.snapshot_id
    assert [model.id for model in verified.models] == [model_id]
    assert verified.access_verifier.audience == "test-audience"


def test_core_public_key_must_match_its_keychain_private_key() -> None:
    private, _ = _key()
    _, wrong_public = _key()
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_peer_key=SecretStr(private),
        failover_core_public_key=wrong_public,
        failover_edge_a_public_key=_key()[1],
        failover_edge_b_public_key=_key()[1],
    )
    with pytest.raises(ValueError, match="does not match"):
        configured_membership(settings)
