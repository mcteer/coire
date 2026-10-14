"""Bounded authenticated transport for one accepted Core plan; no general API client."""

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

import httpx
from opentelemetry import trace
from pydantic import JsonValue

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapStabilityObservation,
    CoreBootstrapHeartbeat,
    CoreBootstrapSeed,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverCommittedRequest,
    CoreHandoverPreparation,
    CoreHandoverRecoveryRequest,
    CoreHandoverRestoreRequest,
    CoreHandoverSummary,
    CoreHandoverTransferIntent,
    CoreHandoverTransferRequest,
    CoreHandoverUnchangedRequest,
    CoreUpgradeInventory,
    ExecutorPassiveReadiness,
    UpgradeActivationConsume,
    UpgradeActivationGrant,
    UpgradeActivationRequest,
    UpgradeClaimIdle,
    UpgradeClaimRequest,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMigrationExecution,
    UpgradeMigrationNotStarted,
    UpgradeOperation,
    UpgradeReceiptAck,
    UpgradeReceiptBatch,
    UpgradeSchemaObservation,
    UpgradeSmokeRequest,
    UpgradeSmokeResult,
    UpgradeWire,
)
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.upgrade_executor.client")


class ExecutorClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        endpoint = httpx.URL(settings.upgrade_api_url)
        if (
            (endpoint.scheme, endpoint.host, endpoint.port, endpoint.raw_path)
            != ("http", "coire-api", 8000, b"/")
            or endpoint.userinfo
            or endpoint.fragment
            or endpoint.query
        ):
            raise UpgradeForbidden("Executor API address differs from its fixed private endpoint")
        token = settings.upgrade_executor_token.get_secret_value()
        if not token.startswith("coire_upgrade_") or len(token) > 4096:
            raise UpgradeUnavailable("Restricted executor credential is unavailable")
        self.settings, self.endpoint = settings, endpoint
        self.authorization = "Bearer " + token
        self.plan: UpgradeExecutionPlan | None = None
        self._authority_sha256: str | None = None
        self._handover_preparation_sha256: str | None = None
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            timeout=5, trust_env=False, follow_redirects=False
        )
        if self._client.follow_redirects or self._client._trust_env:
            raise UpgradeForbidden("Executor transport cannot use redirects or ambient proxies")

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    def bind(self, plan: UpgradeExecutionPlan) -> None:
        if (
            plan.operation.kind is not UpgradeKind.CONTROL_PLANE
            or plan.source.manifest.project != self.settings.upgrade_project
        ):
            raise UpgradeForbidden("Executor transport plan is outside its fixed project")
        if self.plan is not None and self.plan.authority_sha256() != plan.authority_sha256():
            raise UpgradeConflict("Executor transport cannot switch accepted authority")
        self.plan = self.plan or plan
        self._authority_sha256 = self._authority_sha256 or plan.authority_sha256()

    def _prefix(self) -> str:
        if self.plan is None:
            raise UpgradeUnavailable("Executor transport has no accepted operation")
        if self.plan.authority_sha256() != self._authority_sha256:
            raise UpgradeConflict("Executor transport immutable authority changed")
        return f"/api/v1/internal/upgrades/{self.plan.operation.job_id}"

    @tracer.start_as_current_span(
        "coire.upgrade_executor.core_request", record_exception=False, set_status_on_exception=False
    )
    async def _request(
        self,
        method: str,
        path: str,
        body: UpgradeWire | None,
        *,
        deadline: datetime | None = None,
    ) -> dict[str, JsonValue]:
        allowed: dict[tuple[str, str], type[UpgradeWire] | None] = {
            ("POST", "/api/v1/internal/upgrades/claim"): UpgradeClaimRequest,
            ("POST", "/api/v1/internal/upgrades/bootstrap/heartbeat"): CoreBootstrapHeartbeat,
            ("POST", "/api/v1/internal/upgrades/bootstrap/handover-claim"): CoreBootstrapSeed,
            (
                "POST",
                "/api/v1/internal/upgrades/bootstrap/handover-recovery-claim",
            ): CoreBootstrapSeed,
            (
                "POST",
                "/api/v1/internal/upgrades/bootstrap/handover-transfer",
            ): CoreHandoverTransferRequest,
            (
                "POST",
                "/api/v1/internal/upgrades/bootstrap/handover-unchanged",
            ): CoreHandoverUnchangedRequest,
            (
                "POST",
                "/api/v1/internal/upgrades/bootstrap/handover-restoring",
            ): CoreHandoverRecoveryRequest,
            (
                "POST",
                "/api/v1/internal/upgrades/bootstrap/handover-restored",
            ): CoreHandoverRestoreRequest,
            (
                "POST",
                "/api/v1/internal/upgrades/bootstrap/handover-commit",
            ): CoreHandoverCommitRequest,
            (
                "POST",
                "/api/v1/internal/upgrades/bootstrap/handover-committed",
            ): CoreHandoverCommittedRequest,
        }
        if self.plan is None and self._handover_preparation_sha256 is not None:
            allowed[
                (
                    "GET",
                    "/api/v1/internal/upgrades/bootstrap/handover/"
                    + self._handover_preparation_sha256,
                )
            ] = None
        if self.plan is not None:
            prefix = self._prefix()
            allowed |= {
                ("POST", prefix + "/receipts"): UpgradeReceiptBatch,
                ("POST", prefix + "/ready-to-activate"): UpgradeActivationRequest,
                ("POST", prefix + "/consume-activation"): UpgradeActivationConsume,
                ("POST", prefix + "/smoke"): UpgradeSmokeRequest,
                ("GET", prefix + "/migration"): None,
                ("POST", prefix + "/migration-not-started"): None,
                ("GET", prefix + "/schema"): None,
                ("GET", prefix + "/inventory"): None,
                ("GET", prefix): None,
            }
        if (method, path) not in allowed or (
            (body is None) != (allowed[method, path] is None)
            or (body is not None and type(body) is not allowed[method, path])
        ):
            raise UpgradeForbidden("Executor API operation is outside its fixed accepted protocol")
        timeout = 5.0
        if deadline is not None:
            smoke = self.plan is not None and (method, path) == ("POST", self._prefix() + "/smoke")
            recovery = (
                self.plan is None
                and (method, path)
                == ("POST", "/api/v1/internal/upgrades/bootstrap/handover-restoring")
                and isinstance(body, CoreHandoverRecoveryRequest)
                and deadline == body.recovery.deadline_at
            )
            if not smoke and not recovery:
                raise UpgradeForbidden(
                    "Extended request budget is restricted to the accepted smoke"
                )
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise UpgradeForbidden("Original executor request deadline has elapsed")
            timeout = min(120.0 if smoke else 5.0, remaining)
        payload = bytearray()
        content = body.model_dump_json().encode() if body is not None else None
        if content is not None and len(content) > 1024 * 1024:
            raise UpgradeConflict("Executor request exceeds its bound")
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    method,
                    self.endpoint.copy_with(path=path),
                    content=content,
                    timeout=timeout,
                    headers={
                        "Authorization": self.authorization,
                        "Content-Type": "application/json",
                    },
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    raise UpgradeForbidden("Restricted executor request was refused")
                if response.status_code == 409:
                    raise UpgradeConflict("Core refused the accepted executor transition")
                if response.status_code != 200:
                    raise UpgradeUnavailable("Core executor protocol is unavailable")
                async for chunk in response.aiter_bytes(chunk_size=32768):
                    payload.extend(chunk)
                    if len(payload) > 1024 * 1024:
                        raise UpgradeUnavailable("Core executor response exceeds its bound")
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Core executor protocol is unavailable") from None

        def unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate protocol field")
                result[key] = value
            return result

        def finite(value: str) -> None:
            raise ValueError("nonfinite protocol value")

        try:
            decoded = json.loads(payload, object_pairs_hook=unique, parse_constant=finite)
            if not isinstance(decoded, dict):
                raise ValueError("protocol object required")
            return cast(dict[str, JsonValue], decoded)
        except (ValueError, RecursionError):
            raise UpgradeUnavailable("Core executor response is malformed") from None

    async def bootstrap_heartbeat(self, body: CoreBootstrapHeartbeat) -> CoreBootstrapHeartbeat:
        if (
            body.executor_id != self.settings.upgrade_executor_id
            or body.seed.source.manifest.project != self.settings.upgrade_project
        ):
            raise UpgradeForbidden("Bootstrap heartbeat is outside the fixed executor project")
        if not 0 <= (datetime.now(UTC) - body.physical.observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Bootstrap physical observation has expired")
        data = await self._request("POST", "/api/v1/internal/upgrades/bootstrap/heartbeat", body)
        try:
            accepted = CoreBootstrapHeartbeat.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Bootstrap heartbeat acknowledgment is malformed") from None
        if (
            accepted != body
            or not 0 <= (datetime.now(UTC) - accepted.physical.observed_at).total_seconds() <= 5
        ):
            raise UpgradeConflict("Bootstrap acknowledgment changed or expired the original proof")
        return accepted

    async def claim_handover(self, seed: CoreBootstrapSeed) -> CoreHandoverPreparation | None:
        if self.plan is not None or seed.source.manifest.project != self.settings.upgrade_project:
            raise UpgradeForbidden("Executor handover cannot share application authority")
        data = await self._request(
            "POST", "/api/v1/internal/upgrades/bootstrap/handover-claim", seed
        )
        try:
            if data.get("idle") is True:
                UpgradeClaimIdle.model_validate(data)
                return None
            preparation = CoreHandoverPreparation.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Executor handover claim is malformed") from None
        if preparation.incumbent != seed:
            raise UpgradeConflict("Executor handover claim returned another incumbent")
        if not preparation.prepared_at <= datetime.now(UTC) < preparation.deadline_at:
            raise UpgradeUnavailable("Executor handover original deadline elapsed")
        return preparation

    async def claim_handover_recovery(
        self, seed: CoreBootstrapSeed
    ) -> CoreHandoverPreparation | None:
        if self.plan is not None or seed.source.manifest.project != self.settings.upgrade_project:
            raise UpgradeForbidden("Recovery metadata cannot share application authority")
        data = await self._request(
            "POST", "/api/v1/internal/upgrades/bootstrap/handover-recovery-claim", seed
        )
        try:
            if data.get("idle") is True:
                UpgradeClaimIdle.model_validate(data)
                return None
            preparation = CoreHandoverPreparation.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Handover recovery metadata is malformed") from None
        if preparation.incumbent != seed:
            raise UpgradeConflict("Handover recovery metadata belongs to another Source")
        self._bind_handover(preparation)
        return preparation

    def _bind_handover(self, preparation: CoreHandoverPreparation) -> None:
        if (
            self.plan is not None
            or preparation.incumbent.source.manifest.project != self.settings.upgrade_project
        ):
            raise UpgradeForbidden("Executor handover transport is outside its fixed project")
        digest = preparation.sha256()
        if self._handover_preparation_sha256 not in (None, digest):
            raise UpgradeConflict("Executor transport already retains another handover")
        self._handover_preparation_sha256 = digest

    async def handover_status(self, preparation: CoreHandoverPreparation) -> CoreHandoverSummary:
        self._bind_handover(preparation)
        data = await self._request(
            "GET", "/api/v1/internal/upgrades/bootstrap/handover/" + preparation.sha256(), None
        )
        try:
            summary = CoreHandoverSummary.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Executor handover status is malformed") from None
        if (
            summary.project != self.settings.upgrade_project
            or summary.preparation_sha256 != preparation.sha256()
            or summary.generation != preparation.generation
            or summary.candidate_release_id != preparation.candidate_release.release_id
            or summary.incumbent_image_id != preparation.incumbent.executor_image_id
            or summary.candidate_image_id != preparation.candidate_image_id
            or summary.prepared_at != preparation.prepared_at
            or summary.deadline_at != preparation.deadline_at
        ):
            raise UpgradeConflict("Executor handover status changed its retained authority")
        return summary

    async def begin_handover_restoration(
        self, preparation: CoreHandoverPreparation, request: CoreHandoverRecoveryRequest
    ) -> CoreHandoverSummary:
        self._bind_handover(preparation)
        intent = request.recovery
        if (
            intent.preparation_sha256 != preparation.sha256()
            or intent.generation != preparation.generation
            or intent.incumbent_container_id != preparation.incumbent.executor_container_id
        ):
            raise UpgradeForbidden("Executor recovery request differs from its fixed handover")
        data = await self._request(
            "POST",
            "/api/v1/internal/upgrades/bootstrap/handover-restoring",
            request,
            deadline=intent.deadline_at,
        )
        try:
            summary = CoreHandoverSummary.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Executor recovery decision is malformed") from None
        if (
            summary.phase not in {"restoring", "restored"}
            or summary.project != self.settings.upgrade_project
            or summary.preparation_sha256 != preparation.sha256()
            or summary.generation != preparation.generation
            or summary.candidate_release_id != preparation.candidate_release.release_id
            or summary.incumbent_image_id != preparation.incumbent.executor_image_id
            or summary.candidate_image_id != preparation.candidate_image_id
            or summary.prepared_at != preparation.prepared_at
            or summary.deadline_at != preparation.deadline_at
        ):
            raise UpgradeConflict("Executor recovery decision changed its accepted authority")
        return summary

    async def acknowledge_handover_unchanged(
        self,
        preparation: CoreHandoverPreparation,
        request: CoreHandoverUnchangedRequest,
    ) -> CoreHandoverSummary:
        self._bind_handover(preparation)
        if (
            request.preparation_sha256 != preparation.sha256()
            or request.incumbent.seed != preparation.incumbent
            or request.incumbent.executor_id != self.settings.upgrade_executor_id
        ):
            raise UpgradeForbidden("Effect-free failure request differs from its retained Source")
        data = await self._request(
            "POST", "/api/v1/internal/upgrades/bootstrap/handover-unchanged", request
        )
        try:
            summary = CoreHandoverSummary.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Effect-free failure acknowledgment is malformed") from None
        if (
            summary.phase != "failed_unchanged"
            or summary.project != self.settings.upgrade_project
            or summary.preparation_sha256 != preparation.sha256()
            or summary.generation != preparation.generation
            or summary.candidate_release_id != preparation.candidate_release.release_id
            or summary.incumbent_image_id != preparation.incumbent.executor_image_id
            or summary.candidate_image_id != preparation.candidate_image_id
            or summary.prepared_at != preparation.prepared_at
            or summary.deadline_at != preparation.deadline_at
        ):
            raise UpgradeConflict(
                "Effect-free failure acknowledgment changed its retained authority"
            )
        return summary

    async def consume_handover_transfer(
        self, preparation: CoreHandoverPreparation, proof: ExecutorPassiveReadiness
    ) -> CoreHandoverTransferIntent:
        """Return the original consumed intent; its deadline never becomes a new budget.

        The kernel-transfer boundary must check that captured deadline. An expired
        replay remains evidence for reconciliation, never permission for a new transfer.
        """
        self._bind_handover(preparation)
        if (
            self.plan is not None
            or preparation.incumbent.source.manifest.project != self.settings.upgrade_project
            or proof.preparation_sha256 != preparation.sha256()
            or proof.generation != preparation.generation
            or proof.candidate_runtime_id != preparation.candidate_runtime_id
            or proof.candidate_image_id != preparation.candidate_image_id
            or proof.incumbent_runtime_id != preparation.incumbent.executor_runtime_id
            or proof.incumbent_container_id != preparation.incumbent.executor_container_id
        ):
            raise UpgradeForbidden("Executor transfer proof differs from its fixed handover")
        data = await self._request(
            "POST",
            "/api/v1/internal/upgrades/bootstrap/handover-transfer",
            CoreHandoverTransferRequest(
                preparation_sha256=preparation.sha256(), passive_proof_sha256=proof.sha256()
            ),
        )
        try:
            intent = CoreHandoverTransferIntent.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Executor transfer acknowledgment is malformed") from None
        if (
            intent.preparation_sha256 != preparation.sha256()
            or intent.candidate_seed_sha256 != proof.candidate_seed_sha256
            or intent.passive_proof_sha256 != proof.sha256()
            or intent.generation != preparation.generation
            or not preparation.prepared_at <= intent.consumed_at <= datetime.now(UTC)
            or intent.deadline_at > preparation.deadline_at
        ):
            raise UpgradeConflict("Executor transfer acknowledgment changed its captured authority")
        return intent

    async def decide_handover_commit(
        self,
        preparation: CoreHandoverPreparation,
        transfer: CoreHandoverTransferIntent,
        proof: BootstrapStabilityObservation,
    ) -> CoreHandoverCommitIntent:
        self._bind_handover(preparation)
        if (
            self.plan is not None
            or preparation.incumbent.source.manifest.project != self.settings.upgrade_project
            or transfer.preparation_sha256 != preparation.sha256()
            or transfer.generation != preparation.generation
            or proof.generation != preparation.generation
            or proof.incumbent_id != preparation.incumbent.executor_runtime_id
            or proof.incumbent_container_id != preparation.incumbent.executor_container_id
            or proof.incumbent_image_id != preparation.incumbent.executor_image_id
            or proof.candidate_id != preparation.candidate_runtime_id
            or proof.candidate_image_id != preparation.candidate_image_id
        ):
            raise UpgradeForbidden("Executor commit proof differs from its retained handover")
        data = await self._request(
            "POST",
            "/api/v1/internal/upgrades/bootstrap/handover-commit",
            CoreHandoverCommitRequest(preparation_sha256=preparation.sha256(), stability=proof),
        )
        try:
            decision = CoreHandoverCommitIntent.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable(
                "Executor commit decision acknowledgment is malformed"
            ) from None
        if (
            decision.handover_id != transfer.handover_id
            or decision.preparation_sha256 != preparation.sha256()
            or decision.candidate_seed_sha256 != transfer.candidate_seed_sha256
            or decision.stability_sha256 != proof.sha256()
            or decision.generation != preparation.generation
            or not proof.observed_at <= decision.decided_at <= datetime.now(UTC)
        ):
            raise UpgradeConflict("Executor commit acknowledgment changed its captured authority")
        return decision

    async def acknowledge_handover_commit(
        self,
        preparation: CoreHandoverPreparation,
        decision: CoreHandoverCommitIntent,
        request: CoreHandoverCommittedRequest,
    ) -> CoreHandoverSummary:
        self._bind_handover(preparation)
        if (
            self.plan is not None
            or preparation.incumbent.source.manifest.project != self.settings.upgrade_project
            or request.preparation_sha256 != preparation.sha256()
            or decision.preparation_sha256 != preparation.sha256()
            or request.candidate.seed.sha256() != decision.candidate_seed_sha256
            or decision.generation != preparation.generation
            or request.candidate.executor_id != self.settings.upgrade_executor_id
            or request.candidate.ownership.generation != preparation.generation
            or request.incumbent.incumbent_container_id
            != preparation.incumbent.executor_container_id
            or request.incumbent.incumbent_image_id != preparation.incumbent.executor_image_id
        ):
            raise UpgradeForbidden(
                "Executor final commit request differs from its retained selection"
            )
        data = await self._request(
            "POST", "/api/v1/internal/upgrades/bootstrap/handover-committed", request
        )
        try:
            summary = CoreHandoverSummary.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Executor final commit acknowledgment is malformed") from None
        if (
            summary.phase != "committed"
            or summary.handover_id != decision.handover_id
            or summary.project != self.settings.upgrade_project
            or summary.preparation_sha256 != preparation.sha256()
            or summary.generation != preparation.generation
            or summary.candidate_release_id != preparation.candidate_release.release_id
            or summary.incumbent_image_id != preparation.incumbent.executor_image_id
            or summary.candidate_image_id != preparation.candidate_image_id
            or summary.prepared_at != preparation.prepared_at
            or summary.deadline_at != preparation.deadline_at
        ):
            raise UpgradeConflict(
                "Executor final commit acknowledgment changed its accepted authority"
            )
        return summary

    async def acknowledge_handover_restoration(
        self, preparation: CoreHandoverPreparation, request: CoreHandoverRestoreRequest
    ) -> CoreHandoverSummary:
        self._bind_handover(preparation)
        if (
            self.plan is not None
            or preparation.incumbent.source.manifest.project != self.settings.upgrade_project
            or request.preparation_sha256 != preparation.sha256()
            or request.incumbent.seed != preparation.incumbent
            or request.incumbent.executor_id != self.settings.upgrade_executor_id
            or request.candidate.generation != preparation.generation
        ):
            raise UpgradeForbidden(
                "Executor restoration request differs from its retained handover"
            )
        data = await self._request(
            "POST", "/api/v1/internal/upgrades/bootstrap/handover-restored", request
        )
        try:
            summary = CoreHandoverSummary.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Executor restoration acknowledgment is malformed") from None
        if (
            summary.phase != "restored"
            or summary.project != self.settings.upgrade_project
            or summary.preparation_sha256 != preparation.sha256()
            or summary.generation != preparation.generation
            or summary.candidate_release_id != preparation.candidate_release.release_id
            or summary.incumbent_image_id != preparation.incumbent.executor_image_id
            or summary.candidate_image_id != preparation.candidate_image_id
            or summary.prepared_at != preparation.prepared_at
            or summary.deadline_at != preparation.deadline_at
        ):
            raise UpgradeConflict(
                "Executor restoration acknowledgment changed its retained authority"
            )
        return summary

    async def claim(self, source_sha256: str) -> UpgradeExecutionPlan | None:
        request = UpgradeClaimRequest(
            executor_id=self.settings.upgrade_executor_id,
            project=self.settings.upgrade_project,
            observed_source_sha256=source_sha256,
        )
        data = await self._request("POST", "/api/v1/internal/upgrades/claim", request)
        try:
            if data.get("idle") is True:
                UpgradeClaimIdle.model_validate(data)
                return None
            plan = UpgradeExecutionPlan.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core claim response is malformed") from None
        if plan.operation.source_sha256 != source_sha256:
            raise UpgradeConflict("Core claim returned another observed Source")
        self.bind(plan)
        return plan

    def _ack(self, data: dict[str, JsonValue], minimum: int) -> UpgradeReceiptAck:
        if self.plan is None:
            raise UpgradeUnavailable("Executor acknowledgment has no accepted plan")
        try:
            ack = UpgradeReceiptAck.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core acknowledgment is malformed") from None
        if (ack.job_id, ack.generation) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
        ) or ack.sequence < minimum:
            raise UpgradeConflict("Core acknowledgment differs from its accepted durable cursor")
        return ack

    async def receipts(self, batch: UpgradeReceiptBatch) -> UpgradeReceiptAck:
        prefix = self._prefix()
        assert self.plan is not None
        if any(
            (item.job_id, item.generation)
            != (self.plan.operation.job_id, self.plan.operation.generation)
            for item in batch.receipts
        ):
            raise UpgradeConflict("Executor receipt belongs to another accepted operation")
        data = await self._request("POST", prefix + "/receipts", batch)
        return self._ack(data, max(item.sequence for item in batch.receipts))

    async def grant(self, *, migration: bool = False) -> UpgradeActivationGrant:
        prefix = self._prefix()
        assert self.plan is not None
        request = UpgradeActivationRequest(
            generation=self.plan.operation.generation,
            source_sha256=self.plan.operation.source_sha256,
            boundary="migration" if migration else "runtime",
        )
        data = await self._request("POST", prefix + "/ready-to-activate", request)
        try:
            grant = UpgradeActivationGrant.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core activation grant is malformed") from None
        if (grant.job_id, grant.generation, grant.boundary) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            request.boundary,
        ):
            raise UpgradeConflict("Core activation grant belongs to another boundary")
        if not 0 < (grant.expires_at - datetime.now(UTC)).total_seconds() <= 30:
            raise UpgradeConflict("Core activation grant deadline is stale or differs")
        return grant

    async def consume(self, value: UpgradeActivationConsume) -> UpgradeReceiptAck:
        prefix = self._prefix()
        assert self.plan is not None
        if (value.grant.job_id, value.grant.generation, value.intent.observed_sha256) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            self.plan.operation.source_sha256,
        ):
            raise UpgradeConflict("Executor consumed intent differs from accepted authority")
        data = await self._request("POST", prefix + "/consume-activation", value)
        return self._ack(data, value.intent.sequence)

    async def status(self) -> UpgradeOperation:
        data = await self._request("GET", self._prefix(), None)
        assert self.plan is not None
        try:
            operation = UpgradeOperation.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core executor status is malformed") from None
        if operation.authority_sha256() != self.plan.operation.authority_sha256():
            raise UpgradeConflict("Core executor status authority changed")
        return operation

    async def schema(self) -> UpgradeSchemaObservation:
        data = await self._request("GET", self._prefix() + "/schema", None)
        assert self.plan is not None
        try:
            observed = UpgradeSchemaObservation.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core schema observation is malformed") from None
        if (observed.job_id, observed.generation) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
        ) or not 0 <= (datetime.now(UTC) - observed.observed_at).total_seconds() <= 5:
            raise UpgradeConflict("Core schema observation authority or freshness differs")
        return observed

    async def fence_migration_not_started(self) -> UpgradeMigrationNotStarted:
        data = await self._request("POST", self._prefix() + "/migration-not-started", None)
        assert self.plan is not None
        try:
            proof = UpgradeMigrationNotStarted.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core migration fence is malformed") from None
        if (
            proof.job_id,
            proof.generation,
            proof.plan_sha256,
            proof.schema_head,
            proof.schema_sha256,
        ) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            self.plan.authority_sha256(),
            self.plan.initial_schema_head,
            self.plan.initial_schema_sha256,
        ):
            raise UpgradeConflict("Core migration fence differs from accepted Source")
        return proof

    async def migration(self) -> UpgradeMigrationExecution:
        data = await self._request("GET", self._prefix() + "/migration", None)
        assert self.plan is not None
        try:
            proof = UpgradeMigrationExecution.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core migration proof is malformed") from None
        if (proof.job_id, proof.generation, proof.plan_sha256) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            self.plan.authority_sha256(),
        ):
            raise UpgradeConflict("Core migration proof belongs to another accepted plan")
        return proof

    async def inventory(self) -> CoreUpgradeInventory:
        data = await self._request("GET", self._prefix() + "/inventory", None)
        assert self.plan is not None
        try:
            snapshot = CoreUpgradeInventory.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core drain inventory is malformed") from None
        if (snapshot.job_id, snapshot.generation, snapshot.project, snapshot.release_sha256) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            self.plan.source.manifest.project,
            self.plan.operation.source_sha256,
        ) or not 0 <= (datetime.now(UTC) - snapshot.observed_at).total_seconds() <= 5:
            raise UpgradeConflict("Core drain inventory authority or freshness differs")
        return snapshot

    async def smoke(
        self, *, restored: bool = False, deadline: datetime | None = None
    ) -> UpgradeSmokeResult:
        prefix = self._prefix()
        assert self.plan is not None
        request = UpgradeSmokeRequest(
            generation=self.plan.operation.generation, check="source" if restored else "gateway"
        )
        data = await self._request("POST", prefix + "/smoke", request, deadline=deadline)
        try:
            result = UpgradeSmokeResult.model_validate(data)
        except ValueError:
            raise UpgradeUnavailable("Core smoke response is malformed") from None
        release = self.plan.source if restored else self.plan.target
        now = datetime.now(UTC)
        if (
            (
                result.job_id,
                result.generation,
                result.check,
                result.model_id,
                result.model_variant_id,
                result.model_manifest_sha256,
            )
            != (
                self.plan.operation.job_id,
                self.plan.operation.generation,
                request.check,
                self.plan.model_id,
                self.plan.model_variant_id,
                self.plan.model_manifest_sha256,
            )
            or not result.passed
            or result.generated_tokens < 10
            or result.runtime_sha256 != release.manifest_sha256
            or not 0 <= (now - result.finished_at).total_seconds() <= 5
            or not 0 <= (result.finished_at - result.started_at).total_seconds() <= 120
            or (deadline is not None and now >= deadline)
        ):
            raise UpgradeConflict("Core smoke evidence differs from the accepted registry asset")
        return result
