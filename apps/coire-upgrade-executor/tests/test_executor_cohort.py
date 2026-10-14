"""Whole-cohort replacement and restoration follow retained identities across partial failure."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr
from test_executor_journal import activation
from upgrade_fixtures import core_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    COHORT_ORDER,
    CoreExecutorEffect,
    DeploymentOwnership,
    UpgradeDockerContainer,
    UpgradeExecutionPlan,
    UpgradePhase,
    UpgradeSchemaObservation,
    UpgradeWire,
)
from coire_core.settings import Settings
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.executor import CoreExecutor
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


def cohort_plan() -> UpgradeExecutionPlan:
    original = core_plan()
    source_manifest = original.source.manifest.model_copy(
        update={
            "services": tuple(
                original.source.manifest.services[0].model_copy(update={"service": name})
                for name in COHORT_ORDER
            ),
        }
    )
    source = original.source.model_copy(
        update={"manifest": source_manifest, "manifest_sha256": source_manifest.sha256()}
    )
    target_manifest = original.target.manifest.model_copy(
        update={
            "services": tuple(
                original.target.manifest.services[0].model_copy(
                    update={"service": name, "image_id": "sha256:" + "b" * 64}
                )
                for name in COHORT_ORDER
            ),
            "compatibility": original.target.manifest.compatibility.model_copy(
                update={"source_sha256s": (source.manifest_sha256,)}
            ),
        }
    )
    target = original.target.model_copy(
        update={"manifest": target_manifest, "manifest_sha256": target_manifest.sha256()}
    )
    plan = UpgradeExecutionPlan.model_validate(
        original.model_dump()
        | {
            "source": source,
            "target": target,
            "operation": original.operation.model_dump()
            | {
                "cohort": COHORT_ORDER,
                "source_sha256": source.manifest_sha256,
                "target_sha256": target.manifest_sha256,
            },
        }
    )
    return plan


@pytest.mark.parametrize(
    "fault",
    [None, "partial", "source_identity", "candidate_identity", "schema_head", "schema_digest"],
)
async def test_cohort_failure_restores_all_stopped_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    plan = cohort_plan()
    source = plan.source
    journal = ExecutorJournal(tmp_path / "journal", plan)
    now = datetime.now(UTC)
    sources = {
        name: UpgradeDockerContainer(
            container_id=f"{index + 1:064x}",
            name=f"coire-{name}-1",
            service=name,
            project="coire",
            image_id=source.manifest.services[index].image_id,
            observed_at=now,
            running=True,
            health="healthy",
            exit_code=0,
            network_ids={"coire-internal": "b" * 64},
        )
        for index, name in enumerate(COHORT_ORDER)
    }
    journal.freeze_source(tuple(sources.values()), now=now)
    journal.activation(activation(journal, now), now=now)
    settings = Settings(upgrade_executor_token=SecretStr("coire_upgrade_test"))
    docker, client = UpgradeDocker(settings, plan), ExecutorClient(settings)
    assert plan.initial_schema_head is not None and plan.initial_schema_sha256 is not None

    async def schema() -> UpgradeSchemaObservation:
        assert plan.initial_schema_head is not None and plan.initial_schema_sha256 is not None
        return UpgradeSchemaObservation(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            schema_head="0035_candidate" if fault == "schema_head" else plan.initial_schema_head,
            schema_sha256="0" * 64 if fault == "schema_digest" else plan.initial_schema_sha256,
            observed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(client, "schema", AsyncMock(side_effect=schema))
    events: list[tuple[str, str]] = []
    candidates: dict[str, UpgradeDockerContainer] = {}

    async def dispatch(
        effect: CoreExecutorEffect, *_args: object, **_kwargs: object
    ) -> UpgradeDockerContainer:
        assert effect.service is not None
        events.append((effect.action, effect.service))
        if (
            fault == "partial"
            and effect.action == "start_candidate"
            and effect.service == "coire-mcp"
        ):
            raise UpgradeConflict("candidate rejected")
        journal.dispatch_intent(effect.effect_id, authority)
        observed = sources[effect.service].model_copy(update={"observed_at": datetime.now(UTC)})
        if effect.action in {"create_candidate", "start_candidate", "stop_candidate"}:
            observed = observed.model_copy(
                update={
                    "container_id": f"{COHORT_ORDER.index(effect.service) + 100:064x}",
                    "image_id": "sha256:" + "b" * 64,
                    "name": f"coire-upgrade-{plan.operation.job_id.lower()}-{effect.service}",
                    "running": effect.action == "start_candidate",
                }
            )
            candidates[observed.name] = observed
        else:
            observed = observed.model_copy(update={"running": effect.action == "start_source"})
            sources[effect.service] = observed
        journal.observe(effect.effect_id, observed)
        return observed

    async def inspect(name: str, *_args: object, **_kwargs: object) -> UpgradeDockerContainer:
        observed = next(item for item in sources.values() if item.name == name)
        return observed.model_copy(
            update={
                "observed_at": datetime.now(UTC),
                "container_id": "f" * 64 if fault == "source_identity" else observed.container_id,
            }
        )

    for method in ("stop_source", "create_candidate", "candidate_lifecycle", "start_source"):
        monkeypatch.setattr(docker, method, AsyncMock(side_effect=dispatch))
    monkeypatch.setattr(docker, "inspect_verified_source", AsyncMock(side_effect=inspect))

    async def inspect_candidate(
        name: str, *_args: object, **_kwargs: object
    ) -> UpgradeDockerContainer:
        return candidates[name].model_copy(
            update={
                "observed_at": datetime.now(UTC),
                **({"container_id": "f" * 64} if fault == "candidate_identity" else {}),
            }
        )

    monkeypatch.setattr(
        docker, "inspect_verified_candidate", AsyncMock(side_effect=inspect_candidate)
    )

    async def remove_owned(
        effect: CoreExecutorEffect, *_args: object, **_kwargs: object
    ) -> CoreExecutorEffect:
        assert all(source.running and source.health == "healthy" for source in sources.values())
        assert effect.action == "remove_candidate" and effect.service is not None
        assert effect.container_id is not None
        events.append((effect.action, effect.service))
        return journal.observe_removed(effect.effect_id, effect.container_id, datetime.now(UTC))

    monkeypatch.setattr(docker, "remove_candidate", AsyncMock(side_effect=remove_owned))
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )
    try:
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            executor = CoreExecutor(journal, docker, client, authority, tmp_path / "prepared")
            if fault == "partial":
                with pytest.raises(UpgradeConflict):
                    await executor.replace_cohort(dependency_states={})
                changed = COHORT_ORDER[:3]
            else:
                await executor.replace_cohort(dependency_states={})
                changed = COHORT_ORDER
            assert [name for action, name in events if action == "stop_source"] == list(changed)
            if fault == "candidate_identity":
                with pytest.raises(UpgradeConflict, match="owned start"):
                    await executor.selected_runtime(dependency_states={})
            elif fault in {"schema_head", "schema_digest"}:
                with pytest.raises(UpgradeConflict, match="schema"):
                    await executor.selected_runtime(dependency_states={})
            elif fault is None:
                selected = await executor.selected_runtime(dependency_states={})
                assert selected.release_id == plan.target.release_id
                assert selected.host_release_key == plan.target.manifest.host_release_key
                assert {item.container.service for item in selected.services} == set(COHORT_ORDER)
                assert all(
                    item.origin_job_id == plan.operation.job_id for item in selected.services
                )
                # Fresh selection evidence alone must not retire recovery ownership.
                authority.verify()
                replayed = await executor.selected_runtime(dependency_states={})
                assert replayed.model_dump(
                    exclude={
                        "selected_at": True,
                        "services": {"__all__": {"container": {"observed_at"}}},
                    }
                ) == selected.model_dump(
                    exclude={
                        "selected_at": True,
                        "services": {"__all__": {"container": {"observed_at"}}},
                    }
                )
                assert replayed.selected_at >= selected.selected_at
                assert (
                    journal.store.read(
                        "runtime-selection-" + str(selected.release_id) + ".json", type(selected)
                    )
                    == selected
                )
                clock = Mock(wraps=datetime)
                state = journal.state()
                assert state.outage_deadline is not None
                clock.now.return_value = state.outage_deadline + timedelta(seconds=1)
                with monkeypatch.context() as late:
                    late.setattr(__name__ + ".datetime", clock)
                    late.setattr("coire_upgrade_executor.executor.datetime", clock)
                    late.setattr("coire_upgrade_executor.journal.datetime", clock)
                    with pytest.raises(UpgradeUnavailable, match="activation window"):
                        await executor.selected_runtime(dependency_states={})
                    late_selected = await executor.selected_runtime(
                        dependency_states={}, terminal_replay=True
                    )
                    assert late_selected.release_id == plan.target.release_id
            events.clear()
            if fault in {"source_identity", "schema_head", "schema_digest"}:
                if fault == "source_identity":
                    with pytest.raises(UpgradeConflict):
                        await executor.restore_cohort(dependency_states={})
                else:
                    await executor.restore_cohort(dependency_states={})
                with pytest.raises(UpgradeConflict):
                    await executor.cleanup_candidates(dependency_states={})
                assert not any(action == "remove_candidate" for action, _name in events)
            else:
                await executor.restore_cohort(dependency_states={})
                restored = await executor.restored_runtime(dependency_states={})
                assert restored.release_id == plan.source.release_id
                assert {
                    item.container.service: item.container.container_id
                    for item in restored.services
                } == {name: item.container_id for name, item in sources.items()}
                replayed_source = await executor.restored_runtime(dependency_states={})
                assert replayed_source.model_dump(
                    exclude={
                        "selected_at": True,
                        "services": {"__all__": {"container": {"observed_at"}}},
                    }
                ) == restored.model_dump(
                    exclude={
                        "selected_at": True,
                        "services": {"__all__": {"container": {"observed_at"}}},
                    }
                )
                assert replayed_source.selected_at >= restored.selected_at
                assert (
                    journal.store.read(
                        "runtime-selection-" + str(restored.release_id) + ".json", type(restored)
                    )
                    == restored
                )
                authority.verify()
                clean = await executor.cleanup_candidates(dependency_states={})
                assert clean.release_id == plan.source.release_id
                assert [name for action, name in events if action == "remove_candidate"] == list(
                    reversed(changed)
                )
                authority.verify()
            assert [name for action, name in events if action == "start_source"] == list(
                reversed(changed)
            )
            assert all(item.running and item.health == "healthy" for item in sources.values())
            if fault is None:
                deadline = journal.state().recovery_deadline
                assert deadline is not None
                clock = Mock(wraps=datetime)
                clock.now.return_value = deadline + timedelta(seconds=1)
                with monkeypatch.context() as late:
                    late.setattr(__name__ + ".datetime", clock)
                    late.setattr("coire_upgrade_executor.executor.datetime", clock)
                    late.setattr("coire_upgrade_executor.journal.datetime", clock)
                    with pytest.raises(UpgradeUnavailable, match="deadline"):
                        await executor.restored_runtime(dependency_states={})
                    replayed_terminal = await executor.restored_runtime(
                        dependency_states={}, terminal_replay=True
                    )
                    assert replayed_terminal.release_id == plan.source.release_id
                    reader = journal.store.read

                    def no_retained_selection[T: UpgradeWire](key: str, model: type[T]) -> T | None:
                        if key.startswith("runtime-selection-"):
                            return None
                        return reader(key, model)

                    late.setattr(journal.store, "read", no_retained_selection)
                    with pytest.raises(UpgradeUnavailable, match="deadline"):
                        await executor.restored_runtime(dependency_states={}, terminal_replay=True)
                monkeypatch.setattr(client, "status", AsyncMock(return_value=plan.operation))
                with pytest.raises(UpgradeConflict, match="terminal"):
                    await executor.commit_runtime(dependency_states={})
                assert authority.claim.state == "active"
                terminal = plan.operation.model_copy(
                    update={
                        "phase": UpgradePhase.ROLLED_BACK,
                        "finished_at": datetime.now(UTC),
                        "updated_at": datetime.now(UTC),
                    }
                )
                changed_terminal = terminal.model_copy(update={"reason": "candidate_failed"})
                monkeypatch.setattr(
                    client, "status", AsyncMock(side_effect=[terminal, changed_terminal])
                )
                with pytest.raises(UpgradeConflict, match="changed"):
                    await executor.commit_runtime(dependency_states={})
                assert authority.claim.state == "active"
                monkeypatch.setattr(client, "status", AsyncMock(return_value=terminal))
                # This cohort fixture intentionally has no sealed release mount.
                # Physical health alone cannot supply that missing commit authority.
                with pytest.raises(UpgradeUnavailable, match="verified prepared"):
                    await executor.commit_runtime(dependency_states={})
                assert authority.claim.state == "active"
            assert journal.state().recovery_captured_at is not None
    finally:
        await client.close()
        await docker.close()
