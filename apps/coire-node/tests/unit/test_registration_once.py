"""A registration token is distinct from the control token and is consumed once."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from coire_core.settings import Settings
from coire_node.register import Registrar, build_registration_v2


def settings(tmp_path: Path, registration_token: str = "one-time") -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        node_name="coire-edge-a",
        node_control_host="coire-edge-a.lab",
        node_data_host="coire-edge-a.fabric",
        node_state_dir=str(tmp_path),
        node_token=SecretStr("long-lived-control"),
        node_registration_token=SecretStr(registration_token),
    )


def test_registration_uses_separate_credential(tmp_path: Path) -> None:
    registration = build_registration_v2(settings(tmp_path))
    assert registration.token.get_secret_value() == "one-time"


@pytest.mark.asyncio
async def test_success_is_persisted_and_reused_token_is_skipped(tmp_path: Path) -> None:
    current = settings(tmp_path)
    registration = build_registration_v2(current)
    registrar = Registrar(current, registration)

    class Client:
        calls = 0

        async def post(self, *args: object, **kwargs: object) -> SimpleNamespace:
            self.calls += 1
            assert kwargs["json"]["token"] == "one-time"  # type: ignore[index]
            return SimpleNamespace(status_code=200)

    client = Client()
    assert await registrar.register_once(client)  # type: ignore[arg-type]
    assert client.calls == 1
    assert "one-time" not in (tmp_path / "registration-success.sha256").read_text()
    restarted = Registrar(current, registration)
    await restarted.start()
    assert restarted._task is None
    rotated = Registrar(
        settings(tmp_path, "rotated"), build_registration_v2(settings(tmp_path, "rotated"))
    )
    assert not rotated._already_registered()


@pytest.mark.asyncio
async def test_missing_registration_token_does_not_retry(tmp_path: Path) -> None:
    current = settings(tmp_path, "")
    registrar = Registrar(current, build_registration_v2(current))
    await registrar.start()
    assert registrar._task is None
