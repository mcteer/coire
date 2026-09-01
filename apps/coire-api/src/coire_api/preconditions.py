"""Shared optimistic-concurrency precondition handling for admin mutations."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status


def require_version(if_match: str | None) -> datetime:
    if if_match is None:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "If-Match is required")
    try:
        return datetime.fromisoformat(if_match.strip('"').replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "If-Match must be an ISO 8601 timestamp"
        ) from exc


def require_current(if_match: str | None, current: datetime) -> None:
    if require_version(if_match) != current:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "edit_conflict", "current_version": current.isoformat()},
        )
