"""Transaction-bound content-free mutation receipts with per-owner operation keys."""

import hashlib
import uuid

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import EvaluationMutationRow
from coire_core.errors import EvaluationConflict, EvaluationValidationError
from coire_core.models.evaluation import canonical_digest


def key_digest(key: str) -> str:
    if not 1 <= len(key) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise EvaluationValidationError("Idempotency key must be bounded printable ASCII")
    return hashlib.sha256(key.encode()).hexdigest()


async def replay(
    session: AsyncSession, owner: uuid.UUID, operation: str, key: str, request: BaseModel
) -> EvaluationMutationRow | None:
    digest = key_digest(key)
    binding = hashlib.sha256(f"{owner}:{operation}:{digest}".encode()).digest()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(binding[:8], signed=True)},
    )
    row = await session.scalar(
        select(EvaluationMutationRow).where(
            EvaluationMutationRow.owner_user_id == owner,
            EvaluationMutationRow.operation == operation,
            EvaluationMutationRow.key_sha256 == digest,
        )
    )
    if row is not None and row.request_sha256 != canonical_digest(request):
        raise EvaluationConflict("Idempotency key binds a different mutation")
    return row


async def record(
    session: AsyncSession,
    owner: uuid.UUID,
    operation: str,
    key: str,
    request: BaseModel,
    response: BaseModel,
) -> None:
    if len(response.model_dump_json().encode()) > 128 * 1024:
        raise EvaluationValidationError("Mutation receipt exceeds its metadata bound")
    session.add(
        EvaluationMutationRow(
            owner_user_id=owner,
            operation=operation,
            key_sha256=key_digest(key),
            request_sha256=canonical_digest(request),
            response=response.model_dump(mode="json"),
        )
    )
    await session.flush()
