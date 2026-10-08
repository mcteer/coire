"""Content-free mutation telemetry and durable refusal audit after rollback."""

import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from opentelemetry import metrics, trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_core.errors import CoireError
from coire_core.models.audit import AuditOutcome

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.evaluation")
mutations = metrics.get_meter("coire.api.evaluation").create_counter(
    "coire_evaluation_mutations_total"
)
type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
P = ParamSpec("P")
R = TypeVar("R")
operations = metrics.get_meter("coire.api.evaluation").create_counter(
    "coire_evaluation_operations_total"
)


def observed(
    operation: str,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    def decorate(
        function: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with tracer.start_as_current_span(
                operation, record_exception=False, set_status_on_exception=False
            ):
                try:
                    result = await function(*args, **kwargs)
                except Exception:
                    operations.add(1, {"operation": operation, "outcome": "failed"})
                    logger.info("evaluation operation failed", extra={"operation": operation})
                    raise
                operations.add(1, {"operation": operation, "outcome": "completed"})
                return result

        return wrapped

    return decorate


@asynccontextmanager
async def mutation_scope(
    factory: SessionFactory,
    principal: Principal,
    operation: str,
    target: str,
) -> AsyncIterator[AsyncSession]:
    with tracer.start_as_current_span(
        f"coire.api.{operation}", record_exception=False, set_status_on_exception=False
    ):
        try:
            async with factory() as session:
                yield session
        except CoireError as error:
            async with factory() as audit_session:
                await write_principal_audit(
                    audit_session,
                    principal=principal,
                    action="evaluation.refused",
                    target_type="evaluation_operation",
                    target_id=target,
                    outcome=AuditOutcome.REFUSED,
                    context={"operation": operation, "reason": error.code},
                )
            mutations.add(1, {"operation": operation, "outcome": "refused"})
            logger.info(
                "evaluation mutation refused",
                extra={
                    "operation": operation,
                    "safe_reason": error.code,
                    "user_id": str(principal.user_id),
                },
            )
            raise
        mutations.add(1, {"operation": operation, "outcome": "accepted"})
        logger.info(
            "evaluation mutation accepted",
            extra={
                "operation": operation,
                "user_id": str(principal.user_id),
            },
        )
