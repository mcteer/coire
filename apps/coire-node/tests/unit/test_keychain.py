"""Keychain loading (T006).

The agent must not exit when a secret is absent, and must never let an absent secret look like
a configured one.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from coire_core.settings import Settings
from coire_node import keychain


def _settings(**kw: object) -> Settings:
    return Settings(_secrets_dir="/nonexistent", **kw)  # type: ignore[call-arg,arg-type]


def test_absent_items_leave_empty_secrets_and_do_not_raise(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(keychain, "read_item", lambda *a, **k: SecretStr(""))
    settings = _settings()
    keychain.load_node_secrets(settings)
    assert settings.node_token.get_secret_value() == ""
    assert settings.node_registration_token.get_secret_value() == ""
    assert settings.hf_token.get_secret_value() == ""


def test_keychain_values_are_loaded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    values = {
        keychain.NODE_TOKEN_ITEM: "node-tok",
        keychain.NODE_REGISTRATION_TOKEN_ITEM: "register-once",
        keychain.HF_TOKEN_ITEM: "hf_abc",
    }
    monkeypatch.setattr(keychain, "read_item", lambda s, **k: SecretStr(values.get(s, "")))
    settings = _settings()
    keychain.load_node_secrets(settings)
    assert settings.node_token.get_secret_value() == "node-tok"
    assert settings.node_registration_token.get_secret_value() == "register-once"
    assert settings.hf_token.get_secret_value() == "hf_abc"


def test_configured_values_win_over_the_keychain(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Containers and CI supply secrets by environment and have no keychain to consult."""
    monkeypatch.setattr(keychain, "read_item", lambda *a, **k: SecretStr("from-keychain"))
    settings = _settings(
        node_token=SecretStr("from-env"),
        node_registration_token=SecretStr("registration-from-env"),
        hf_token=SecretStr("hf-from-env"),
    )
    keychain.load_node_secrets(settings)
    assert settings.node_token.get_secret_value() == "from-env"
    assert settings.node_registration_token.get_secret_value() == "registration-from-env"
    assert settings.hf_token.get_secret_value() == "hf-from-env"


def test_failover_lifecycle_and_monitor_are_loaded_without_node_token_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "coire-node-token": "normal-node",
        "coire-failover-relay-token": "peer-relay",
        "coire-failover-monitor-token": "edge-monitor",
        "coire-failover-lifecycle-token": "local-lifecycle",
    }
    monkeypatch.setattr(
        keychain, "read_item", lambda service, **_: SecretStr(values.get(service, ""))
    )
    settings = _settings()
    keychain.load_node_secrets(settings)
    assert settings.failover_monitor_token.get_secret_value() == "edge-monitor"
    assert settings.failover_lifecycle_token.get_secret_value() == "local-lifecycle"
    assert settings.failover_relay_token.get_secret_value() == "peer-relay"
    assert settings.node_token.get_secret_value() == "normal-node"


def test_read_item_strips_the_trailing_newline(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`security -w` prints a trailing newline; a token carrying one fails every compare."""
    import subprocess

    class Done:
        returncode = 0
        stdout = "tok-with-newline\n"

    monkeypatch.setattr("coire_node.keychain.shutil.which", lambda _: "/usr/bin/security")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert keychain.read_item("x").get_secret_value() == "tok-with-newline"


def test_read_item_returns_empty_off_macos(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("coire_node.keychain.shutil.which", lambda _: None)
    assert keychain.read_item("x").get_secret_value() == ""


def test_isolated_node_does_not_read_the_cluster_registration_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []

    def read(service: str, **_kwargs: object) -> SecretStr:
        reads.append(service)
        return SecretStr("fixture-only")

    monkeypatch.setattr(keychain, "read_item", read)
    settings = _settings(node_registration_enabled=False)
    keychain.load_node_secrets(settings)
    assert keychain.NODE_REGISTRATION_TOKEN_ITEM not in reads
    assert settings.node_registration_token.get_secret_value() == ""
    assert keychain.NODE_TOKEN_ITEM in reads
