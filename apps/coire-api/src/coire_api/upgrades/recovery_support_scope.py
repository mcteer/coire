"""Non-wire request scope of an immutable compatibility API instance."""

from contextvars import ContextVar

from coire_core.errors import UpgradeForbidden

scope: ContextVar[tuple[str, int, str] | None] = ContextVar(
    "coire_recovery_support_scope", default=None
)


def require_original(job_id: str, generation: int, source_sha256: str) -> None:
    expected = scope.get()
    if expected is not None and expected != (job_id, generation, source_sha256):
        raise UpgradeForbidden("Compatibility API is scoped to another original failure")
