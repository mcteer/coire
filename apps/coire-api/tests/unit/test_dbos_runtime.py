from __future__ import annotations

from unittest.mock import patch

from pydantic import SecretStr

from coire_core.settings import Settings
from coire_scheduler.dbos_runtime import DBOSRuntime, dbos_database_url


def test_dbos_uses_sync_psycopg_database_url() -> None:
    settings = Settings(postgres_password=SecretStr("secret"))
    url = dbos_database_url(settings)
    assert url.startswith("postgresql+psycopg://")
    assert "asyncpg" not in url


def test_runtime_launch_and_destroy_are_idempotent() -> None:
    runtime = DBOSRuntime(Settings(postgres_password=SecretStr("secret")))
    with patch("coire_scheduler.dbos_runtime.DBOS") as dbos:
        runtime.launch()
        runtime.launch()
        dbos.assert_called_once()
        dbos.launch.assert_called_once()
        runtime.destroy()
        runtime.destroy()
        dbos.destroy.assert_called_once_with(workflow_completion_timeout_sec=10)
