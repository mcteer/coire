"""Typed upgrade SDK; administration stays on the authenticated gateway API."""

import asyncio
from typing import Self
from uuid import UUID

import httpx
from pydantic import BaseModel, SecretStr, TypeAdapter

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeNotFound, UpgradeUnavailable
from coire_core.models.upgrade_native_terminal import (
    UpgradeNativeTerminalGrant,
    UpgradeNativeTerminalRequest,
)
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryCancelRequest,
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryRequest,
    UpgradeExpiredRecoveryStatus,
    UpgradeExpiredRecoveryUnstartedCloseRequest,
    UpgradeExpiredRecoveryUnstartedClosure,
    UpgradeRecoveryHostHoldReceipt,
    UpgradeRecoveryHostHoldRequest,
    UpgradeRecoverySchemaObservation,
    UpgradeRecoverySchemaReceipt,
    UpgradeRecoverySchemaRequest,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationRead,
    UpgradeColdContinuationRequest,
    UpgradeColdContinuationSchemaObservation,
    UpgradeColdContinuationSchemaReceipt,
    UpgradeColdContinuationSchemaRequest,
)
from coire_core.models.upgrade_unclaimed import (
    UpgradeUnclaimedCancellationGrant,
    UpgradeUnclaimedCancellationRequest,
)
from coire_core.models.upgrades import (
    CoreHandoverStartRequest,
    CoreHandoverSummary,
    StudioName,
    UpgradeCancelRequest,
    UpgradeEventPage,
    UpgradeId,
    UpgradeKind,
    UpgradeOperation,
    UpgradeReleasePage,
    UpgradeReleaseRegister,
    UpgradeReleaseSummary,
    UpgradeRollbackRequest,
    UpgradeStartRequest,
    UpgradeTargetList,
)


class UpgradeClient:
    def __init__(
        self,
        api_url: str,
        token: SecretStr,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        origin = httpx.URL(api_url)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.host
            or origin.userinfo
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Upgrade API origin is invalid")
        if not token.get_secret_value() or len(token.get_secret_value()) > 4096:
            raise ValueError("Upgrade API credential is absent or exceeds its bound")
        self.origin = str(origin).rstrip("/")
        self._token = token
        self._client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request[T: BaseModel](
        self,
        method: str,
        suffix: str,
        response_model: type[T],
        body: BaseModel | None = None,
        *,
        params: dict[str, str | int] | None = None,
    ) -> T:
        try:
            async with (
                asyncio.timeout(5),
                self._client.stream(
                    method,
                    self.origin + "/api/v1/admin/upgrades" + suffix,
                    headers={
                        "Authorization": "Bearer " + self._token.get_secret_value(),
                        "Accept": "application/json",
                    },
                    json=body.model_dump(mode="json") if body is not None else None,
                    params=params,
                    timeout=5,
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    raise UpgradeForbidden("Upgrade API denied the administrator credential")
                if response.status_code == 404:
                    raise UpgradeNotFound("Upgrade API resource is absent")
                if response.status_code == 409:
                    raise UpgradeConflict("Upgrade prerequisites or accepted authority differ")
                if response.status_code not in {200, 201, 202}:
                    raise UpgradeUnavailable("Upgrade API request is unavailable")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > 1024 * 1024:
                        raise UpgradeUnavailable("Upgrade API response exceeds its bound")
                    payload.extend(chunk)
                return response_model.model_validate_json(payload)
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Upgrade API transport is unavailable") from None

    async def releases(
        self, *, cursor: str | None = None, kind: UpgradeKind | None = None
    ) -> UpgradeReleasePage:
        params: dict[str, str | int] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if kind is not None:
            params["kind"] = kind.value
        return await self._request("GET", "/releases", UpgradeReleasePage, params=params)

    async def register(self, request: UpgradeReleaseRegister) -> UpgradeReleaseSummary:
        return await self._request("POST", "/releases", UpgradeReleaseSummary, request)

    async def targets(self) -> UpgradeTargetList:
        return await self._request("GET", "/targets", UpgradeTargetList)

    async def prepare_native_terminal(
        self, recovery_id: str, request: UpgradeNativeTerminalRequest
    ) -> UpgradeNativeTerminalGrant:
        recovery_id = TypeAdapter(UpgradeId).validate_python(recovery_id)
        result = await self._request(
            "POST",
            "/recoveries/" + recovery_id + "/native-terminal",
            UpgradeNativeTerminalGrant,
            request,
        )
        owner = result.resolution.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
        if (
            owner.intent.recovery_id != recovery_id
            or result.resolution.sha256() != request.resolution_sha256
        ):
            raise UpgradeConflict("Upgrade API acknowledged another canonical native resolution")
        return result

    async def request_unclaimed_cancellation(
        self, job_id: str, request: UpgradeUnclaimedCancellationRequest
    ) -> UpgradeUnclaimedCancellationGrant:
        job_id = TypeAdapter(UpgradeId).validate_python(job_id)
        result = await self._request(
            "POST",
            "/" + job_id + "/unclaimed-cancellation",
            UpgradeUnclaimedCancellationGrant,
            request,
        )
        if (
            result.plan.operation.job_id != job_id
            or result.plan.authority_sha256() != request.plan_sha256
        ):
            raise UpgradeConflict("Upgrade API acknowledged another unclaimed preparation")
        return result

    async def reconcile_recovery_host_hold(
        self, recovery_id: str, request: UpgradeRecoveryHostHoldRequest
    ) -> UpgradeRecoveryHostHoldReceipt:
        recovery_id = TypeAdapter(UpgradeId).validate_python(recovery_id)
        result = await self._request(
            "POST",
            "/recoveries/" + recovery_id + "/host-hold",
            UpgradeRecoveryHostHoldReceipt,
            request,
        )
        if (
            result.recovery_id != recovery_id
            or result.resolution_sha256 != request.resolution_sha256
        ):
            raise UpgradeConflict("Upgrade API acknowledged another terminal host hold")
        return result

    async def create_recovery(
        self, request: UpgradeExpiredRecoveryRequest
    ) -> UpgradeExpiredRecoveryIntent:
        intent = await self._request("POST", "/recoveries", UpgradeExpiredRecoveryIntent, request)
        if intent.request != request:
            raise UpgradeConflict("Upgrade API acknowledged another recovery request")
        return intent

    async def continue_cold_recovery(
        self, request: UpgradeColdContinuationRequest
    ) -> UpgradeExpiredRecoveryIntent:
        result = await self._request(
            "POST", "/recoveries/continuations", UpgradeExpiredRecoveryIntent, request
        )
        if result.request != request.request or result.recovery_id == request.previous_recovery_id:
            raise UpgradeConflict("Upgrade API acknowledged another cold continuation")
        return result

    async def cold_continuation_schema(
        self, recovery_id: str, request: UpgradeColdContinuationRead
    ) -> UpgradeColdContinuationSchemaObservation:
        recovery_id = TypeAdapter(UpgradeId).validate_python(recovery_id)
        result = await self._request(
            "POST",
            "/recoveries/" + recovery_id + "/continuation-schema-observation",
            UpgradeColdContinuationSchemaObservation,
            request,
        )
        if (
            result.previous_recovery_id != recovery_id
            or result.previous_intent_sha256 != request.previous_intent_sha256
            or result.previous_owner_sha256 != request.previous_owner_sha256
        ):
            raise UpgradeConflict("Upgrade API acknowledged another cold schema scope")
        return result

    async def prepare_cold_continuation_schema(
        self, request: UpgradeColdContinuationSchemaRequest
    ) -> UpgradeColdContinuationSchemaReceipt:
        result = await self._request(
            "POST",
            "/recoveries/" + request.previous_recovery_id + "/continuation-schema",
            UpgradeColdContinuationSchemaReceipt,
            request,
        )
        if (
            result.request_sha256 != request.sha256()
            or result.previous_recovery_id != request.previous_recovery_id
            or result.previous_intent_sha256 != request.previous_intent_sha256
            or result.previous_owner_sha256 != request.previous_owner_sha256
        ):
            raise UpgradeConflict("Upgrade API acknowledged another cold schema preparation")
        return result

    async def recovery_status(self, recovery_id: str) -> UpgradeExpiredRecoveryStatus:
        recovery_id = TypeAdapter(UpgradeId).validate_python(recovery_id)
        result = await self._request(
            "GET", "/recoveries/" + recovery_id, UpgradeExpiredRecoveryStatus
        )
        if result.intent.recovery_id != recovery_id:
            raise UpgradeConflict("Upgrade API acknowledged another recovery")
        return result

    async def cancel_recovery(
        self, recovery_id: str, request: UpgradeExpiredRecoveryCancelRequest
    ) -> UpgradeExpiredRecoveryCancellation:
        recovery_id = TypeAdapter(UpgradeId).validate_python(recovery_id)
        result = await self._request(
            "POST",
            "/recoveries/" + recovery_id + "/cancel",
            UpgradeExpiredRecoveryCancellation,
            request,
        )
        if result.recovery_id != recovery_id or result.intent_sha256 != request.intent_sha256:
            raise UpgradeConflict("Upgrade API acknowledged another recovery cancellation")
        return result

    async def close_unstarted_recovery(
        self, recovery_id: str, request: UpgradeExpiredRecoveryUnstartedCloseRequest
    ) -> UpgradeExpiredRecoveryUnstartedClosure:
        recovery_id = TypeAdapter(UpgradeId).validate_python(recovery_id)
        result = await self._request(
            "POST",
            "/recoveries/" + recovery_id + "/close-unstarted",
            UpgradeExpiredRecoveryUnstartedClosure,
            request,
        )
        if result.recovery_id != recovery_id or result.request_sha256 != request.sha256():
            raise UpgradeConflict("Upgrade API acknowledged another unstarted recovery closure")
        return result

    async def recovery_schema(self, job_id: str) -> UpgradeRecoverySchemaObservation:
        job_id = TypeAdapter(UpgradeId).validate_python(job_id)
        result = await self._request(
            "GET", "/recovery-schema/" + job_id, UpgradeRecoverySchemaObservation
        )
        if result.original_job_id != job_id:
            raise UpgradeConflict("Upgrade API acknowledged another schema scope")
        return result

    async def prepare_recovery_schema(
        self, request: UpgradeRecoverySchemaRequest
    ) -> UpgradeRecoverySchemaReceipt:
        result = await self._request(
            "POST", "/recovery-schema", UpgradeRecoverySchemaReceipt, request
        )
        if result.request != request:
            raise UpgradeConflict("Upgrade API acknowledged another schema preparation")
        return result

    async def start_handover(self, request: CoreHandoverStartRequest) -> CoreHandoverSummary:
        result = await self._request("POST", "/executor-handover", CoreHandoverSummary, request)
        if result.candidate_release_id != request.release_id:
            raise UpgradeConflict("Upgrade API acknowledged another executor release")
        return result

    async def handover_status(self, handover_id: UUID) -> CoreHandoverSummary:
        result = await self._request(
            "GET", "/executor-handover/" + str(handover_id), CoreHandoverSummary
        )
        if result.handover_id != handover_id:
            raise UpgradeConflict("Upgrade API acknowledged another executor handover")
        return result

    async def start_studio(
        self, node: StudioName, request: UpgradeStartRequest
    ) -> UpgradeOperation:
        node = TypeAdapter(StudioName).validate_python(node)
        operation = await self._request("POST", "/nodes/" + node, UpgradeOperation, request)
        self._started(operation, request)
        if (operation.kind, operation.node) != (UpgradeKind.STUDIO, node):
            raise UpgradeConflict("Upgrade API acknowledged another target")
        return operation

    async def start_core(self, request: UpgradeStartRequest) -> UpgradeOperation:
        operation = await self._request("POST", "/control-plane", UpgradeOperation, request)
        self._started(operation, request)
        if operation.kind is not UpgradeKind.CONTROL_PLANE:
            raise UpgradeConflict("Upgrade API acknowledged another target")
        return operation

    @staticmethod
    def _started(operation: UpgradeOperation, request: UpgradeStartRequest) -> None:
        if (operation.target_release_id, operation.source_sha256, operation.idempotency_key) != (
            request.release_id,
            request.expected_source_sha256,
            request.idempotency_key,
        ):
            raise UpgradeConflict("Upgrade API acknowledgment differs from the start request")

    async def status(self, job_id: str) -> UpgradeOperation:
        job_id = TypeAdapter(UpgradeId).validate_python(job_id)
        operation = await self._request("GET", "/" + job_id, UpgradeOperation)
        if operation.job_id != job_id:
            raise UpgradeConflict("Upgrade API acknowledged another job")
        return operation

    async def cancel(self, job_id: str, request: UpgradeCancelRequest) -> UpgradeOperation:
        job_id = TypeAdapter(UpgradeId).validate_python(job_id)
        operation = await self._request("POST", "/" + job_id + "/cancel", UpgradeOperation, request)
        if operation.job_id != job_id:
            raise UpgradeConflict("Upgrade API acknowledged another job")
        return operation

    async def rollback(self, job_id: str, request: UpgradeRollbackRequest) -> UpgradeOperation:
        job_id = TypeAdapter(UpgradeId).validate_python(job_id)
        operation = await self._request(
            "POST", "/" + job_id + "/rollback", UpgradeOperation, request
        )
        if (operation.source_sha256, operation.idempotency_key) != (
            request.expected_source_sha256,
            request.idempotency_key,
        ) or operation.parent_job_id != job_id:
            raise UpgradeConflict("Upgrade API acknowledgment differs from the rollback request")
        return operation

    async def events(self, job_id: str, *, after: int = 0) -> UpgradeEventPage:
        job_id = TypeAdapter(UpgradeId).validate_python(job_id)
        if not 0 <= after <= 2**63 - 1:
            raise ValueError("Upgrade event cursor exceeds its bound")
        return await self._request(
            "GET", "/" + job_id + "/events", UpgradeEventPage, params={"after": after}
        )
