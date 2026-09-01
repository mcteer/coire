"""DBOS lifecycle owned exclusively by the scheduler process."""

from __future__ import annotations

import logging

from dbos import DBOS

from coire_core.settings import Settings

logger = logging.getLogger(__name__)


def dbos_database_url(settings: Settings) -> str:
    """Return DBOS's synchronous psycopg URL without logging the secret."""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


class DBOSRuntime:
    """Small testable lifecycle boundary around DBOS's process-global runtime."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.launched = False

    def launch(self) -> None:
        if self.launched:
            return
        DBOS(
            config={
                "name": "coire-scheduler",
                "system_database_url": dbos_database_url(self.settings),
                "application_version": self.settings.service_version,
                "run_admin_server": False,
                "enable_otlp": False,
                "use_listen_notify": True,
            }
        )
        DBOS.launch()
        self.launched = True
        logger.info("DBOS launched; pending workflows recovered")

    def destroy(self) -> None:
        if not self.launched:
            return
        DBOS.destroy(workflow_completion_timeout_sec=10)
        self.launched = False
        logger.info("DBOS stopped")
