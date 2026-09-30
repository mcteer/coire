"""Safe, typed domain failures for native chat boundaries."""

from __future__ import annotations

from coire_core.models.gateway import ProblemDetails


class CoireError(Exception):
    """Expected failure whose public form contains no engine or parser diagnostics."""

    code = "coire_error"
    title = "Request failed"
    status = 500

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title

    def to_problem(self) -> ProblemDetails:
        """Map to the platform's RFC 9457 response shape."""

        return ProblemDetails(
            type=f"urn:coire:{self.code}",
            title=self.title,
            status=self.status,
            detail=self.detail,
            coire_code=self.code,
        )


class ChatNotFound(CoireError):
    code = "chat_not_found"
    title = "Chat item not found"
    status = 404


class ChatForbidden(CoireError):
    code = "chat_forbidden"
    title = "Chat access denied"
    status = 403


class ChatConflict(CoireError):
    code = "chat_conflict"
    title = "Chat state conflict"
    status = 409


class ChatQuotaExceeded(CoireError):
    code = "chat_quota_exceeded"
    title = "Chat file limit exceeded"
    status = 413


class ChatUnsupportedFile(CoireError):
    code = "chat_unsupported_file"
    title = "Unsupported file"
    status = 415


class ChatContextExceeded(CoireError):
    code = "chat_context_exceeded"
    title = "Context limit exceeded"
    status = 422


class ChatModelUnavailable(CoireError):
    code = "chat_model_unavailable"
    title = "Selected model unavailable"
    status = 503
