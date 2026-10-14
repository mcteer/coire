"""The bounded local health probe preserves authentication without server startup work."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from coire_core.settings import Settings
from coire_upgrade_executor import healthcheck


@pytest.mark.parametrize("status", [200, 302, 401, 503])
def test_probe_checks_only_authenticated_local_liveness(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    connection = Mock()
    connection.getresponse.return_value = SimpleNamespace(status=status)
    factory = Mock(return_value=connection)
    monkeypatch.setattr(healthcheck, "HTTPConnection", factory)
    settings = Settings(upgrade_executor_token=SecretStr("scoped-probe-fixture"))
    assert healthcheck.check(settings) == (0 if status == 200 else 1)
    factory.assert_called_once_with("127.0.0.1", 8011, timeout=2)
    connection.request.assert_called_once_with(
        "GET", "/live", headers={"Authorization": "Bearer scoped-probe-fixture"}
    )
    connection.close.assert_called_once()


@pytest.mark.parametrize("value", [None, "", "x" * 4097, "unsafe\nheader"])
def test_probe_requires_bounded_mounted_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | None
) -> None:
    monkeypatch.setattr(healthcheck, "DEFAULT_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("UPGRADE_EXECUTOR_TOKEN", "inherited-must-not-substitute")
    if value is not None:
        (tmp_path / "upgrade_executor_token").write_text(value)
    factory = Mock(side_effect=AssertionError("uncredentialed probe contacted service"))
    monkeypatch.setattr(healthcheck, "HTTPConnection", factory)
    assert healthcheck.check() == 1
    factory.assert_not_called()


def test_probe_mounted_credential_outranks_environment_and_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(healthcheck, "DEFAULT_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("UPGRADE_EXECUTOR_TOKEN", "wrong-inherited-secret")
    (tmp_path / "upgrade_executor_token").write_text(" mounted-fixture-secret\n")
    connection = Mock()
    connection.getresponse.side_effect = TimeoutError()
    monkeypatch.setattr(healthcheck, "HTTPConnection", Mock(return_value=connection))
    assert healthcheck.check() == 1
    connection.request.assert_called_once_with(
        "GET", "/live", headers={"Authorization": "Bearer mounted-fixture-secret"}
    )
    connection.close.assert_called_once()


def test_probe_cold_import_does_not_load_server_or_settings_graph() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import coire_upgrade_executor.healthcheck; "
            "assert not any(name in sys.modules for name in "
            "('fastapi', 'httpx', 'pydantic', 'coire_core.settings', "
            "'coire_core.models.upgrades', 'coire_upgrade_executor.main'))",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
