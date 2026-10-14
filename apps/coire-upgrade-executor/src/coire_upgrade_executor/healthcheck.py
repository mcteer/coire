"""Credentialed local probe without importing the recovery server or settings graph."""

from __future__ import annotations

from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import TYPE_CHECKING

from coire_core.secret_paths import DEFAULT_SECRETS_DIR

if TYPE_CHECKING:
    from coire_core.settings import Settings


def check(settings: Settings | None = None) -> int:
    if settings is not None:
        token = settings.upgrade_executor_token.get_secret_value()
    else:
        try:
            # The container probe uses its mounted scoped secret. Inherited
            # environment credentials cannot substitute for that mount.
            with (Path(DEFAULT_SECRETS_DIR) / "upgrade_executor_token").open("rb") as secret:
                token = secret.read(4097).decode().strip()
        except (OSError, UnicodeError):
            return 1
    if not token or len(token) > 4096 or "\r" in token or "\n" in token:
        return 1
    connection = HTTPConnection("127.0.0.1", 8011, timeout=2)
    try:
        connection.request("GET", "/live", headers={"Authorization": f"Bearer {token}"})
        return 0 if connection.getresponse().status == 200 else 1
    except (OSError, HTTPException, ValueError):
        return 1
    finally:
        connection.close()
