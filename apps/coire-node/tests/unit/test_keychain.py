"""Keychain loading (T006).

The agent must not exit when a secret is absent, and must never let an absent secret look like
a configured one.
"""

from __future__ import annotations

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
    assert settings.hf_token.get_secret_value() == ""


def test_keychain_values_are_loaded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    values = {keychain.NODE_TOKEN_ITEM: "node-tok", keychain.HF_TOKEN_ITEM: "hf_abc"}
    monkeypatch.setattr(keychain, "read_item", lambda s, **k: SecretStr(values.get(s, "")))
    settings = _settings()
    keychain.load_node_secrets(settings)
    assert settings.node_token.get_secret_value() == "node-tok"
    assert settings.hf_token.get_secret_value() == "hf_abc"


def test_configured_values_win_over_the_keychain(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Containers and CI supply secrets by environment and have no keychain to consult."""
    monkeypatch.setattr(keychain, "read_item", lambda *a, **k: SecretStr("from-keychain"))
    settings = _settings(node_token=SecretStr("from-env"), hf_token=SecretStr("hf-from-env"))
    keychain.load_node_secrets(settings)
    assert settings.node_token.get_secret_value() == "from-env"
    assert settings.hf_token.get_secret_value() == "hf-from-env"


def test_read_item_strips_the_trailing_newline(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`security -w` prints a trailing newline; a token carrying one fails every compare."""
    import subprocess

    class Done:
        returncode = 0
        stdout = "tok-with-newline\n"

    monkeypatch.setattr(keychain.shutil, "which", lambda _: "/usr/bin/security")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert keychain.read_item("x").get_secret_value() == "tok-with-newline"


def test_read_item_returns_empty_off_macos(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(keychain.shutil, "which", lambda _: None)
    assert keychain.read_item("x").get_secret_value() == ""
