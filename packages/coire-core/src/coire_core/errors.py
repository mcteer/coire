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


class TrainingNotFound(CoireError):
    code = "training_not_found"
    title = "Training resource not found"
    status = 404


class TrainingForbidden(CoireError):
    code = "training_forbidden"
    title = "Training access denied"
    status = 403


class TrainingConflict(CoireError):
    code = "training_conflict"
    title = "Training state conflict"
    status = 409


class TrainingValidationError(CoireError):
    code = "training_validation_error"
    title = "Invalid training input"
    status = 422


class TrainingUploadTooLarge(CoireError):
    code = "training_upload_too_large"
    title = "Training input exceeds its size bound"
    status = 413


class TrainingQuotaExceeded(CoireError):
    code = "training_quota_exceeded"
    title = "Training quota exceeded"
    status = 429


class TrainingUnavailable(CoireError):
    code = "training_unavailable"
    title = "Training dependency unavailable"
    status = 503


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


class ImageInvalidCursor(CoireError):
    code = "image_invalid_cursor"
    title = "Invalid image event cursor"
    status = 400


class ImageInputTooLarge(CoireError):
    code = "image_input_too_large"
    title = "Image input too large"
    status = 413


class ImageUnsupportedInput(CoireError):
    code = "image_unsupported_input"
    title = "Unsupported image input"
    status = 415


class ImageTimeout(CoireError):
    code = "image_timeout"
    title = "Image generation timed out"
    status = 504

    def __init__(self, job_id: str) -> None:
        super().__init__("accepted image work continues")
        self.job_id = job_id


class ImageGenerationFailed(CoireError):
    code = "image_generation_failed"
    title = "Image generation failed"
    status = 409

    def __init__(self, job_id: str, failure_code: str) -> None:
        super().__init__(failure_code)
        self.job_id = job_id


class ImageQuotaExceeded(CoireError):
    code = "image_quota_exceeded"
    title = "Image allowance exceeded"
    status = 429


class ImageStorageUnavailable(CoireError):
    code = "image_storage_unavailable"
    title = "Image storage unavailable"
    status = 507


class EvaluationNotFound(CoireError):
    code = "evaluation_not_found"
    title = "Evaluation resource not found"
    status = 404


class EvaluationForbidden(CoireError):
    code = "evaluation_forbidden"
    title = "Evaluation requires an active admin owner"
    status = 403


class EvaluationConflict(CoireError):
    code = "evaluation_conflict"
    title = "Evaluation state conflict"
    status = 409


class EvaluationSelfJudge(EvaluationConflict):
    code = "evaluation_self_judge"
    title = "Judge cannot evaluate its own base model"


class EvaluationExecutionRequired(EvaluationConflict):
    code = "evaluation_execution_required"
    title = "Submit a platform execution at /api/v1/admin/evaluations"


class EvaluationValidationError(CoireError):
    code = "evaluation_validation_error"
    title = "Invalid evaluation request"
    status = 422


class EvaluationDisabled(CoireError):
    code = "evaluation_admission_disabled"
    title = "New evaluation admission is disabled"
    status = 503


class EvaluationUnavailable(CoireError):
    code = "evaluation_unavailable"
    title = "Evaluation runtime unavailable"
    status = 503


class EvaluationQuotaExceeded(CoireError):
    code = "evaluation_quota_exceeded"
    title = "Evaluation queue or evidence quota exhausted"
    status = 429


class EvaluationEvidenceGone(CoireError):
    code = "evaluation_evidence_expired"
    title = "Raw evaluation evidence is unavailable"
    status = 410


class FeedbackNotFound(CoireError):
    code = "feedback_not_found"
    title = "Feedback resource not found"
    status = 404


class FeedbackForbidden(CoireError):
    code = "feedback_forbidden"
    title = "Feedback access denied"
    status = 403


class FeedbackConflict(CoireError):
    code = "feedback_conflict"
    title = "Feedback state conflict"
    status = 409


class FeedbackQuotaExceeded(CoireError):
    code = "feedback_quota_exceeded"
    title = "Feedback quota exceeded"
    status = 429


class FeedbackValidationError(CoireError):
    code = "feedback_validation_error"
    title = "Invalid feedback input"
    status = 422


class FeedbackUnavailable(CoireError):
    code = "feedback_unavailable"
    title = "Exact comparison target is unavailable"
    status = 503
