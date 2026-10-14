"""Candidate protocol proof requires its sealed incarnation and a retained real incumbent."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from test_handover_observer import prepared_executor
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import CoreHandoverPreparation
from coire_core.settings import Settings
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.ownership import bootstrap_ownership
from coire_upgrade_executor.passive import CandidatePassiveObserver


@pytest.mark.parametrize(
    "fault", [None, "expired", "absent_incumbent", "wrong_incumbent", "stale", "future"]
)
async def test_passive_protocol_cannot_claim_a_writer_or_renew_original_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import coire_upgrade_executor.passive as module

    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    value = core_plan()
    source, _ = prepared_executor(tmp_path, value.source, "sha256:" + "e" * 64)
    candidate, _ = prepared_executor(tmp_path, value.target, "sha256:" + "f" * 64)
    seed, source_physical = prepared_bootstrap(source)
    now = datetime.now(UTC)
    prepared_at = now - timedelta(seconds=121) if fault == "expired" else now
    request = CoreHandoverPreparation(
        incumbent=seed,
        candidate_release=candidate,
        candidate_runtime_id="candidate-executor",
        candidate_image_id="sha256:" + "f" * 64,
        generation=2,
        initiator_user_id=candidate.prepared_by,
        idempotency_key=uuid4(),
        prepared_at=prepared_at,
        deadline_at=prepared_at + timedelta(seconds=120),
    )
    journal = HandoverCreationJournal(tmp_path / "creation", request)
    effect = journal.begin(now=prepared_at)
    journal.observe(effect, "a" * 64, observed_at=prepared_at, now=prepared_at)
    candidate_seed = journal.candidate_seed()
    physical = source_physical.model_copy(
        update={
            "seed_sha256": candidate_seed.sha256(),
            "executor_runtime_id": candidate_seed.executor_runtime_id,
            "executor_container_id": candidate_seed.executor_container_id,
            "executor_image_id": candidate_seed.executor_image_id,
        }
    )
    if fault in {"stale", "future"}:
        captured = now - timedelta(seconds=6) if fault == "stale" else now + timedelta(seconds=1)
        physical = physical.model_copy(update={"observed_at": captured})
    observed = AsyncMock(return_value=physical)
    monkeypatch.setattr(
        module, "InitialBootstrapObserver", lambda *args: SimpleNamespace(observe=observed)
    )
    settings = Settings(
        upgrades_enabled=True,
        upgrade_prepared_dir=str(tmp_path / "prepared"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        with bootstrap_ownership(
            Path(settings.upgrade_coordination_dir),
            seed,
            source_physical,
            settings.upgrade_executor_id,
        ) as authority:
            real = idle_executor_owner
            if fault == "absent_incumbent":
                monkeypatch.setattr(module, "idle_executor_owner", lambda *args: None)
            elif fault == "wrong_incumbent":
                monkeypatch.setattr(
                    module,
                    "idle_executor_owner",
                    lambda *args: authority.claim.model_copy(
                        update={"executor_runtime_id": "other"}
                    ),
                )
            observer = CandidatePassiveObserver(settings, journal, client)
            if fault is None:
                proof = await observer.observe()
                assert proof.preparation_sha256 == request.sha256()
                assert proof.candidate_seed_sha256 == candidate_seed.sha256()
                assert proof.observed_at == physical.observed_at
                assert not proof.recovery_ready and not proof.exclusive_writer_held
            else:
                with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                    await observer.observe()
            assert (
                real(authority.store, settings.upgrade_project, settings.upgrade_executor_id)
                == authority.claim
            )
            authority.verify()
            if fault in {"expired", "absent_incumbent", "wrong_incumbent"}:
                observed.assert_not_awaited()
