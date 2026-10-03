"""Authenticated typed scheduler client for the private CPU file worker."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx
from pydantic import ValidationError

from coire_core.models.files import (
    FileProcessCancel,
    FileProcessRequest,
    FileProcessStatus,
    FilePurgeResult,
    ImageFileProcessRequest,
    ImageFileProcessResult,
    is_ulid,
)
from coire_core.models.image_worker import ImageRecipeParseRequest, ImageRecipeParseResult
from coire_core.settings import Settings


class FileWorkerError(RuntimeError):
    """Content-free worker communication failure; never includes HTTP body or credentials."""


class FileWorkerBusy(FileWorkerError):
    pass


class FileWorkerMissing(FileWorkerError):
    pass


class FileWorkerParseRefused(FileWorkerError):
    """A staged input failed the worker's private parser validation."""


class FileWorkerClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client
        self._owned = client is None

    async def __aenter__(self) -> FileWorkerClient:
        if self.client is None:
            self.client = httpx.AsyncClient(base_url=self.settings.file_worker_url, timeout=5.0)
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if self._owned and self.client is not None:
            await self.client.aclose()
        self.client = None

    async def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> FileProcessStatus:
        token = self.settings.file_worker_service_token.get_secret_value()
        if not token or self.client is None:
            raise FileWorkerError("worker unavailable")
        try:
            response = await self.client.request(
                method, path, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError:
            raise FileWorkerError("worker unavailable") from None
        if response.status_code == 429:
            raise FileWorkerBusy("worker busy")
        if response.status_code == 404:
            raise FileWorkerMissing("worker job missing")
        if response.status_code not in {200, 202}:
            raise FileWorkerError("worker refused request")
        try:
            return FileProcessStatus.model_validate(response.json())
        except (ValueError, ValidationError):
            raise FileWorkerError("invalid worker response") from None

    async def process(self, request: FileProcessRequest) -> FileProcessStatus:
        status = await self._call("POST", "/v1/process", request.model_dump(mode="json"))
        if status.job_id != request.job_id:
            raise FileWorkerError("worker job identity mismatch")
        return status

    async def parse_image_recipe(self, request: ImageRecipeParseRequest) -> ImageRecipeParseResult:
        """Parse one staged recipe, requiring exact response binding before persistence."""
        token = self.settings.file_worker_service_token.get_secret_value()
        if not token or self.client is None:
            raise FileWorkerError("recipe worker unavailable")
        try:
            response = await self.client.post(
                "/v1/image-recipes/parse",
                json=request.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.settings.file_worker_process_timeout_s + 5.0,
            )
        except httpx.HTTPError:
            raise FileWorkerError("recipe worker unavailable") from None
        if response.status_code == 429:
            raise FileWorkerBusy("recipe worker busy")
        if response.status_code == 422:
            raise FileWorkerParseRefused("recipe worker refused input")
        if response.status_code != 200:
            raise FileWorkerError("recipe worker unavailable")
        try:
            result = ImageRecipeParseResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise FileWorkerError("invalid recipe worker response") from None
        if (
            result.input_id != request.input_id
            or result.source_sha256 != request.source_sha256
            or result.byte_count != request.byte_count
        ):
            raise FileWorkerError("recipe worker identity mismatch")
        return result

    async def process_image_input(self, request: ImageFileProcessRequest) -> ImageFileProcessResult:
        """Normalize one exact owner input in the isolated CPU worker."""
        token = self.settings.file_worker_service_token.get_secret_value()
        if not token or self.client is None:
            raise FileWorkerError("image input worker unavailable")
        try:
            response = await self.client.post(
                "/v1/image-inputs/process",
                json=request.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.settings.file_worker_process_timeout_s + 5.0,
            )
        except httpx.HTTPError:
            raise FileWorkerError("image input worker unavailable") from None
        if response.status_code == 429:
            raise FileWorkerBusy("image input worker busy")
        if response.status_code == 422:
            raise FileWorkerParseRefused("image input worker refused input")
        if response.status_code != 200:
            raise FileWorkerError("image input worker unavailable")
        try:
            result = ImageFileProcessResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise FileWorkerError("invalid image input worker response") from None
        if (
            result.job_id != request.job_id
            or result.input_id != request.input_id
            or result.operation != request.operation
            or result.source_sha256 != request.source_sha256
            or result.output_id != request.output_id
        ):
            raise FileWorkerError("image input worker identity mismatch")
        return result

    async def status(self, job_id: str) -> FileProcessStatus:
        if not is_ulid(job_id):
            raise ValueError("invalid worker job ID")
        status = await self._call("GET", f"/v1/jobs/{job_id}")
        if status.job_id != job_id:
            raise FileWorkerError("worker job identity mismatch")
        return status

    async def cancel(self, job_id: str) -> FileProcessStatus:
        if not is_ulid(job_id):
            raise ValueError("invalid worker job ID")
        body = FileProcessCancel(job_id=job_id)
        status = await self._call("POST", f"/v1/jobs/{job_id}/cancel", body.model_dump(mode="json"))
        if status.job_id != job_id:
            raise FileWorkerError("worker job identity mismatch")
        return status

    async def purge(self, job_id: str) -> FilePurgeResult:
        if not is_ulid(job_id):
            raise ValueError("invalid worker job ID")
        if self.client is None:
            raise FileWorkerError("worker unavailable")
        token = self.settings.file_worker_service_token.get_secret_value()
        if not token:
            raise FileWorkerError("worker unavailable")
        try:
            response = await self.client.request(
                "DELETE",
                f"/v1/jobs/{job_id}/output",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            raise FileWorkerError("worker unavailable") from None
        if response.status_code == 409:
            raise FileWorkerBusy("worker output active")
        if response.status_code != 200:
            raise FileWorkerError("worker purge unavailable")
        try:
            result = FilePurgeResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise FileWorkerError("invalid worker purge response") from None
        if result.job_id != job_id:
            raise FileWorkerError("worker job identity mismatch")
        return result
