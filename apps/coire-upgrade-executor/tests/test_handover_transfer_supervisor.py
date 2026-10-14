"""Transfer orchestration retains the real Source kernel on denial/interruption.

Docker/HTTP observations are fixtures here; their physical boundaries have separate tests.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from test_handover_creation_journal import preparation
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.upgrades import (
    CoreBootstrapPhysicalObservation,
    CoreHandoverCandidateStart,
    CoreHandoverCreation,
    CoreHandoverPreparation,
    CoreHandoverTransferIntent,
    ExecutorPassiveReadiness,
)
from coire_core.settings import Settings
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_transfer_supervisor import prepare_writer_transfer
from coire_upgrade_executor.ownership import bootstrap_ownership
from coire_upgrade_executor.source_observer import InitialBootstrapObserver


@pytest.mark.parametrize("fault", [None, "revoked", "interrupted", "already_stopping"])
async def test_consumed_transfer_never_releases_its_incumbent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import coire_upgrade_executor.handover_preparation as preparation_module
    import coire_upgrade_executor.handover_transfer_supervisor as module

    value = preparation()
    _, physical = prepared_bootstrap(value.incumbent.source)
    physical = physical.model_copy(update={"seed_sha256": value.incumbent.sha256()})
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    root = tmp_path / "state/bootstrap/handover" / str(value.generation)
    stop = asyncio.Event()
    if fault == "already_stopping":
        stop.set()

    async def create(observation: CoreBootstrapPhysicalObservation) -> CoreHandoverCreation:
        assert observation == physical
        retained = HandoverCreationJournal.open_retained(root, value)
        effect = retained.begin()
        retained.dispatch(effect)
        return retained.observe(effect, "a" * 64, observed_at=datetime.now(UTC))

    async def start(observation: CoreBootstrapPhysicalObservation) -> CoreHandoverCandidateStart:
        retained = HandoverCreationJournal.open_retained(root, value)
        effect = retained.begin_start()
        retained.dispatch_start(effect)
        now = datetime.now(UTC)
        return retained.observe_start(started_at=now, observed_at=now)

    docker = Mock(
        create=AsyncMock(side_effect=create), start_candidate=AsyncMock(side_effect=start)
    )
    factory = Mock(return_value=docker)
    monkeypatch.setattr(preparation_module, "HandoverDocker", factory)
    monkeypatch.setattr(module, "HandoverDocker", factory)

    async def passive(_docker: object) -> ExecutorPassiveReadiness:
        retained = HandoverCreationJournal.open_retained(root, value)
        seed = retained.retained_candidate_seed()
        return ExecutorPassiveReadiness(
            preparation_sha256=value.sha256(),
            candidate_seed_sha256=seed.sha256(),
            generation=value.generation,
            candidate_runtime_id=seed.executor_runtime_id,
            candidate_container_id=seed.executor_container_id,
            candidate_image_id=seed.executor_image_id,
            incumbent_runtime_id=value.incumbent.executor_runtime_id,
            incumbent_container_id=value.incumbent.executor_container_id,
            observed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(module, "observe_passive_protocol", passive)
    source = Mock(spec=InitialBootstrapObserver)
    source.observe = AsyncMock(return_value=physical)
    api = Mock(spec=ExecutorClient)
    api.plan = None
    api.bootstrap_heartbeat = AsyncMock()

    async def consume(
        preparation: CoreHandoverPreparation, proof: ExecutorPassiveReadiness
    ) -> CoreHandoverTransferIntent:
        assert preparation == value
        if fault == "revoked":
            raise UpgradeForbidden("actor revoked at atomic consume")
        now = datetime.now(UTC)
        if fault == "interrupted":
            stop.set()
        return CoreHandoverTransferIntent(
            handover_id=uuid4(),
            intent_id=uuid4(),
            preparation_sha256=value.sha256(),
            candidate_seed_sha256=proof.candidate_seed_sha256,
            passive_proof_sha256=proof.sha256(),
            generation=value.generation,
            consumed_at=now,
            deadline_at=now + timedelta(seconds=30),
        )

    api.consume_handover_transfer = AsyncMock(side_effect=consume)
    async with httpx.AsyncClient(trust_env=False) as http:
        with bootstrap_ownership(
            Path(settings.upgrade_coordination_dir),
            value.incumbent,
            physical,
            settings.upgrade_executor_id,
        ) as authority:
            publish_handover_hold(settings, value)
            if fault:
                with pytest.raises((UpgradeConflict, UpgradeForbidden)):
                    await prepare_writer_transfer(
                        settings, value, authority, source, api, http, stop
                    )
            else:
                intent = await prepare_writer_transfer(
                    settings, value, authority, source, api, http, stop
                )
                retained = HandoverCreationJournal.open_retained(root, value)
                assert retained.retained_transfer() == intent
            authority.verify()
            assert (
                idle_executor_owner(authority.store, "coire", settings.upgrade_executor_id)
                == authority.claim
            )
            if fault == "already_stopping":
                factory.assert_not_called()
                source.observe.assert_not_awaited()
            else:
                retained = HandoverCreationJournal.open_retained(root, value)
                assert retained.store.path("transfer-intent.json").exists() == (fault != "revoked")
                docker.start_candidate.assert_awaited_once()
