"""Core steps use durable progress and preserve physical recovery before API replay."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr
from upgrade_failover_fixtures import ArmedCoreProbe
from upgrade_fixtures import core_plan

from coire_api.upgrades.state import validate_progress
from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreRuntimeSelection,
    CoreSelectedService,
    CoreServiceWindow,
    CoreUpgradeInventory,
    DeploymentOwnership,
    UpgradeActivationConsume,
    UpgradeActivationGrant,
    UpgradeDockerContainer,
    UpgradeEvidence,
    UpgradeOwnerInventory,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeReceiptAck,
    UpgradeReceiptBatch,
    UpgradeSchemaObservation,
    UpgradeSmokeResult,
    core_upgrade_required_owners,
)
from coire_core.settings import Settings
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.executor import CoreExecutor
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership
from coire_upgrade_executor.workflow import advance_core_step


@pytest.mark.parametrize(
    "failure, api_timeout, revoked, rejected, service_timeout, commit_timeout",
    [
        (False, False, False, False, False, False),
        (True, False, False, False, False, False),
        (False, True, False, False, False, False),
        (False, False, True, False, False, False),
        (False, False, False, True, False, False),
        (False, True, False, False, True, False),
        (False, False, False, False, False, True),
    ],
)
async def test_core_steps_follow_actual_progress_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
    api_timeout: bool,
    revoked: bool,
    rejected: bool,
    service_timeout: bool,
    commit_timeout: bool,
) -> None:
    plan = core_plan()
    journal = ExecutorJournal(tmp_path / "journal", plan)
    settings = Settings(upgrade_executor_token=SecretStr("coire_upgrade_test"))
    docker, client = UpgradeDocker(settings, plan), ExecutorClient(settings)
    operation = plan.operation
    history: list[UpgradeReceipt] = []
    events: list[str] = []
    failover = ArmedCoreProbe(monkeypatch)

    def ingest(receipt: UpgradeReceipt, *, activation: bool = False) -> None:
        nonlocal operation
        if receipt.sequence <= len(history):
            assert history[receipt.sequence - 1] == receipt
            return
        assert receipt.sequence == len(history) + 1
        validate_progress(operation, receipt, plan, history, activation_authorized=activation)
        history.append(receipt)
        terminal = receipt.phase in {UpgradePhase.SUCCEEDED, UpgradePhase.ROLLED_BACK}
        operation = operation.model_copy(
            update={
                "phase": receipt.phase,
                "receipt_sequence": receipt.sequence,
                "updated_at": datetime.now(UTC),
                "finished_at": datetime.now(UTC) if terminal else None,
            }
        )

    async def receipts(batch: UpgradeReceiptBatch) -> UpgradeReceiptAck:
        for receipt in batch.receipts:
            ingest(receipt)
        return UpgradeReceiptAck(
            job_id=operation.job_id, generation=operation.generation, sequence=len(history)
        )

    async def consume(value: UpgradeActivationConsume) -> UpgradeReceiptAck:
        ingest(value.intent, activation=True)
        return UpgradeReceiptAck(
            job_id=operation.job_id, generation=operation.generation, sequence=len(history)
        )

    async def status() -> object:
        events.append("status")
        if (
            rejected
            and operation.phase is UpgradePhase.ACTIVATING
            and journal.state().recovery_captured_at is None
        ):
            raise UpgradeConflict("progress rejected")
        if (
            revoked
            and operation.phase is UpgradePhase.ACTIVATING
            and journal.state().recovery_captured_at is None
        ):
            raise UpgradeForbidden("restricted credential revoked")
        if (
            api_timeout
            and operation.phase is UpgradePhase.VERIFYING
            and journal.state().recovery_captured_at is None
        ):
            state = journal.state()
            expired_at = datetime.now(UTC) - timedelta(seconds=1)
            journal.store.write(
                "epoch.json",
                state.model_copy(
                    update={
                        "readiness_deadline": datetime.now(UTC) + timedelta(seconds=90)
                        if service_timeout
                        else datetime.now(UTC) - timedelta(seconds=1),
                        "service_windows": (
                            CoreServiceWindow(
                                service=operation.cohort[-1],
                                captured_at=expired_at - timedelta(seconds=120),
                                deadline_at=expired_at,
                            ),
                        )
                        if service_timeout
                        else state.service_windows,
                    }
                ),
            )
            raise UpgradeUnavailable("gateway unavailable")
        return operation

    monkeypatch.setattr(client, "receipts", AsyncMock(side_effect=receipts))
    monkeypatch.setattr(client, "consume", AsyncMock(side_effect=consume))
    monkeypatch.setattr(client, "status", AsyncMock(side_effect=status))
    monkeypatch.setattr(
        client,
        "grant",
        AsyncMock(
            return_value=UpgradeActivationGrant(
                job_id=operation.job_id,
                generation=operation.generation,
                boundary="runtime",
                grant_id=uuid4(),
                expires_at=datetime.now(UTC) + timedelta(seconds=30),
            )
        ),
    )
    monkeypatch.setattr(
        client,
        "inventory",
        AsyncMock(
            return_value=CoreUpgradeInventory(
                job_id=operation.job_id,
                generation=operation.generation,
                project="coire",
                release_sha256=plan.source.manifest_sha256,
                observed_at=datetime.now(UTC),
                owners=tuple(
                    UpgradeOwnerInventory(owner=owner, status="complete")
                    for owner in sorted(core_upgrade_required_owners(plan))
                ),
            )
        ),
    )

    def selected(source: bool) -> CoreRuntimeSelection:
        observed_at = datetime.now(UTC)
        release = plan.source if source else plan.target
        role = release.manifest.services[0]
        assert release.manifest.compose_sha256 is not None
        assert release.manifest.host_release_key is not None
        return CoreRuntimeSelection(
            project="coire",
            release_id=release.release_id,
            manifest_sha256=release.manifest_sha256,
            compose_sha256=release.manifest.compose_sha256,
            host_release_key=release.manifest.host_release_key,
            owner_identity="source",
            generation=1,
            selected_at=observed_at,
            services=(
                CoreSelectedService(
                    container=UpgradeDockerContainer(
                        container_id=("a" if source else "b") * 64,
                        name="coire-coire-api-1",
                        project="coire",
                        service=role.service,
                        image_id=role.image_id,
                        observed_at=observed_at,
                        running=True,
                        health="healthy",
                        exit_code=0,
                        network_ids={"coire-internal": "c" * 64},
                    )
                ),
            ),
        )

    async def schema(*, restored: bool) -> UpgradeSchemaObservation:
        assert plan.initial_schema_head is not None and plan.initial_schema_sha256 is not None
        return UpgradeSchemaObservation(
            job_id=operation.job_id,
            generation=operation.generation,
            schema_head=plan.initial_schema_head,
            schema_sha256=plan.initial_schema_sha256,
            observed_at=datetime.now(UTC),
        )

    async def smoke(
        *, restored: bool = False, deadline: datetime | None = None
    ) -> UpgradeSmokeResult:
        assert deadline is not None and datetime.now(UTC) < deadline
        events.append("source-smoke" if restored else "target-smoke")
        now = datetime.now(UTC)
        return UpgradeSmokeResult(
            job_id=operation.job_id,
            generation=operation.generation,
            check="source" if restored else "gateway",
            runtime_sha256=(plan.source if restored else plan.target).manifest_sha256,
            model_id=plan.model_id,
            model_variant_id=plan.model_variant_id,
            model_manifest_sha256=plan.model_manifest_sha256,
            generated_tokens=10,
            passed=True,
            started_at=now,
            finished_at=now,
            output_sha256="f" * 64,
        )

    monkeypatch.setattr(client, "smoke", AsyncMock(side_effect=smoke))
    monkeypatch.setattr(
        docker,
        "inspect_verified_source",
        AsyncMock(return_value=selected(True).services[0].container),
    )
    monkeypatch.setattr(docker, "preflight_cohort", AsyncMock())
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=operation.job_id,
        operation_generation=operation.generation,
        source_sha256=operation.source_sha256,
    )
    try:
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            executor = CoreExecutor(journal, docker, client, authority, tmp_path / "prepared")
            monkeypatch.setattr(executor, "verify_schema", AsyncMock(side_effect=schema))
            monkeypatch.setattr(
                executor, "selected_runtime", AsyncMock(side_effect=lambda **_: selected(False))
            )
            monkeypatch.setattr(
                executor, "restored_runtime", AsyncMock(side_effect=lambda **_: selected(True))
            )
            monkeypatch.setattr(
                executor, "cleanup_candidates", AsyncMock(side_effect=lambda **_: selected(True))
            )
            restore = AsyncMock()
            publication_calls = 0

            async def publish(**_: object) -> None:
                nonlocal publication_calls
                publication_calls += 1
                if commit_timeout and publication_calls == 1:
                    raise UpgradeUnavailable("publication acknowledgment lost")

            commit = AsyncMock(side_effect=publish)
            monkeypatch.setattr(executor, "restore_cohort", restore)
            monkeypatch.setattr(executor, "commit_runtime", commit)
            replace = AsyncMock(
                side_effect=UpgradeConflict("candidate failed") if failure else None
            )
            monkeypatch.setattr(executor, "replace_cohort", replace)
            for _ in range(18):
                await failover.probe(tmp_path / "coordination")
                try:
                    await advance_core_step(executor)
                except UpgradeConflict:
                    assert (
                        failure or rejected
                    ) and journal.state().recovery_captured_at is not None
                except UpgradeUnavailable:
                    assert api_timeout or commit_timeout
                    if commit_timeout:
                        assert journal.state().recovery_captured_at is None
                if commit.await_count and operation.finished_at is not None:
                    break
            recovering = failure or api_timeout or revoked or rejected
            assert commit.await_count == (1 if recovering else 3 if commit_timeout else 2)
            assert operation.phase is (
                UpgradePhase.ROLLED_BACK if recovering else UpgradePhase.SUCCEEDED
            )
            assert journal.receipts.acknowledged_sequence() == operation.receipt_sequence
            assert all(
                receipt.evidence == "" or UpgradeEvidence.model_validate_json(receipt.evidence)
                for receipt in history
            )
            if recovering:
                restore.assert_awaited_once()
                assert "source-smoke" in events and "target-smoke" not in events
                if revoked or rejected:
                    replace.assert_not_awaited()
            else:
                restore.assert_not_awaited()
                assert "target-smoke" in events and "source-smoke" not in events
            # This is an orchestration contract test; publication is covered by
            # physical-selection tests, and bootstrap readiness stays false.
            assert authority.claim.state == "active"
            # Even terminal SQL progress cannot release the still-active physical
            # writer. Signed peer liveness remains reachable through every stage.
            await failover.probe(tmp_path / "coordination")
            assert failover.probes >= 3 and failover.reached == 0
    finally:
        await failover.close()
        await client.close()
        await docker.close()
