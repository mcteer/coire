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


class ImageNotFound(CoireError):
    code = "image_not_found"
    title = "Image item not found"
    status = 404


class ImageForbidden(CoireError):
    code = "image_forbidden"
    title = "Image access denied"
    status = 403


class ImageConflict(CoireError):
    code = "image_conflict"
    title = "Image state conflict"
    status = 409


class ImageValidationError(CoireError):
    code = "image_validation_error"
    title = "Invalid image request"
    status = 422


class ImageInputTooLarge(CoireError):
    code = "image_input_too_large"
    title = "Image input too large"
    status = 413


class ImageUnsupportedInput(CoireError):
    code = "image_unsupported_input"
    title = "Unsupported image input"
    status = 415


class ImageQuotaExceeded(CoireError):
    code = "image_quota_exceeded"
    title = "Image allowance exceeded"
    status = 429


class ImageStorageUnavailable(CoireError):
    code = "image_storage_unavailable"
    title = "Image storage unavailable"
    status = 507
