"""Dedicated private scheduler credential for every file-worker route."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, status

from coire_core.settings import Settings


def require_service_token(authorization: str | None, settings: Settings) -> None:
    expected = settings.file_worker_service_token.get_secret_value()
    if not expected or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    supplied = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
