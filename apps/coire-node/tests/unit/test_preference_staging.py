"""Versioned pair assets and checkpoint decisions retain fenced node ownership."""

import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest
from test_preference_analysis_unit import Tokenizer
from test_preference_compilation import frozen

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import (
    DatasetInputGrant,
    TrainingInputSource,
    TrainingInputsRequest,
    TrainingLeaseRenewal,
    TrainingStartRequest,
)
from coire_core.settings import Settings
from coire_node.training import rendering
from coire_node.training.journal import TrainingJournal
from coire_node.training.preference_data import PreferenceFrozenInputs, compile_preference_samples
from coire_node.training.supervisor import TrainingSupervisor
from coire_node.training.worker import (
    load_training_frozen_inputs,
    make_frozen_inputs,
    validate_training_frozen_inputs,
)


@pytest.mark.asyncio
async def test_preparation_lease_can_renew_without_resurrecting_stopped_or_expired_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    command, _ = frozen(tmp_path)
    command.lease_expires_at = datetime.now(UTC) + timedelta(seconds=10)
    journal = TrainingJournal(
        tmp_path / "journal", node=command.node, admission_lock=threading.RLock()
    )
    supervisor = TrainingSupervisor(
        journal, interpreter=Path("/opt/coire/envs/v1/bin/python"), validate_ready=lambda _: None
    )
    try:
        journal.prepare(
            command, memory_available=16 * 1024**2, disk_available=32 * 1024**2, disk_floor=0
        )
        renewal = TrainingLeaseRenewal.model_validate(
            {
                **{
                    key: value
                    for key, value in command.model_dump(mode="json").items()
                    if key in TrainingLeaseRenewal.model_fields
                },
                "command_id": str(uuid.uuid4()),
                "lease_expires_at": datetime.now(UTC) + timedelta(seconds=29),
            }
        )
        await supervisor.renew(renewal)
        assert journal.get(command.attempt_id)["liveness"] == "prepared"
        assert journal.get(command.attempt_id)["pid"] is None
        assert not (supervisor.directory(command.attempt_id) / "renew.json").exists()
        assert (
            journal.get(command.attempt_id)["lease_expires_at"]
            == renewal.lease_expires_at.isoformat()
        )
        from coire_node.training import journal as journal_module

        class Later(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> "Later":
                return cls.fromtimestamp(
                    (command.lease_expires_at + timedelta(seconds=1)).timestamp(), tz=UTC
                )

        monkeypatch.setattr(journal_module, "datetime", Later)
        receipt = await supervisor.prepare(
            command, memory_available=16 * 1024**2, disk_available=32 * 1024**2, disk_floor=0
        )
        assert receipt.ready
        for state in ["stopped", "prepared"]:
            with journal.transaction():
                value = journal.get(command.attempt_id)
                value["liveness"] = state
                if state == "prepared":
                    value["lease_expires_at"] = (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat()
                journal.save(value)
            with pytest.raises(TrainingConflict, match="resurrect"):
                await supervisor.renew(
                    renewal.model_copy(
                        update={
                            "command_id": uuid.uuid4(),
                            "lease_expires_at": datetime.now(UTC) + timedelta(seconds=30),
                        }
                    )
                )
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_prepare_poll_reuses_validation_but_start_rechecks_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    command, inputs = frozen(tmp_path)
    journal = TrainingJournal(
        tmp_path / "journal", node=command.node, admission_lock=threading.RLock()
    )
    validations = 0

    def validate(prepared: object) -> None:
        nonlocal validations
        validations += 1
        load_training_frozen_inputs(command, supervisor.directory(command.attempt_id), journal)

    supervisor = TrainingSupervisor(
        journal, interpreter=Path("/opt/coire/envs/v1/bin/python"), validate_ready=validate
    )
    try:
        journal.prepare(
            command, memory_available=16 * 1024**2, disk_available=32 * 1024**2, disk_floor=0
        )
        for source in inputs:
            await supervisor.bind_inputs(command, source, multi=True, disk_available=32 * 1024**2)
        for _ in range(2):
            receipt = await supervisor.prepare(
                command, memory_available=16 * 1024**2, disk_available=32 * 1024**2, disk_floor=0
            )
            assert receipt.ready
        delivery = TrainingInputsRequest.model_validate(
            {
                **{
                    key: value
                    for key, value in command.model_dump(mode="json").items()
                    if key in TrainingInputsRequest.model_fields
                },
                "command_id": uuid.uuid4(),
                "sources": [
                    TrainingInputSource(
                        binding=source.binding,
                        split=source.split,
                        analysis=source.analysis,
                        grant=DatasetInputGrant(
                            grant_id=uuid.uuid4(),
                            node=command.node,
                            dataset_id=source.binding.dataset_id,
                            source_sha256=source.binding.source_sha256,
                            max_bytes=source.source.stat().st_size,
                            attempt_id=command.attempt_id,
                            expires_at=command.lease_expires_at,
                            secret="synthetic-fixture-grant-secret-0001",
                        ),
                    )
                    for source in inputs
                ],
            }
        )
        for _ in range(2):
            assert (await supervisor.submit_inputs(delivery, settings=Settings())).ready
        assert validations == 1
        staged = load_training_frozen_inputs(
            command, supervisor.directory(command.attempt_id), journal
        )
        staged[0].source.write_bytes(b'{"changed":true}\n')
        start = TrainingStartRequest.model_validate(
            {
                **{
                    key: value
                    for key, value in command.model_dump(mode="json").items()
                    if key in TrainingStartRequest.model_fields
                },
                "command_id": "00000000-0000-4000-8000-000000000001",
                "prepared_command_id": command.command_id,
                "spawn_nonce": "00000000-0000-4000-8000-000000000002",
            }
        )
        with pytest.raises(TrainingConflict, match="digest"):
            await supervisor.start(start)
        assert validations == 2
        assert journal.get(command.attempt_id)["pid"] is None
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_private_v3_input_staging_replay_and_changed_source_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    command, inputs = frozen(tmp_path)
    journal = TrainingJournal(
        tmp_path / "journal", node=command.node, admission_lock=threading.RLock()
    )
    supervisor = TrainingSupervisor(
        journal, interpreter=Path("/opt/coire/envs/v1/bin/python"), validate_ready=lambda _: None
    )
    try:
        journal.prepare(
            command, memory_available=16 * 1024**2, disk_available=32 * 1024**2, disk_floor=0
        )
        initial = journal.held_bytes()[1]
        for source in inputs:
            await supervisor.bind_inputs(command, source, multi=True, disk_available=32 * 1024**2)
        held = journal.held_bytes()[1]
        assert held > initial
        for source in inputs:
            await supervisor.bind_inputs(command, source, multi=True, disk_available=32 * 1024**2)
        assert journal.held_bytes()[1] == held
        staged = load_training_frozen_inputs(
            command, supervisor.directory(command.attempt_id), journal
        )
        assert all(isinstance(source, PreferenceFrozenInputs) for source in staged)
        paired = [source for source in staged if isinstance(source, PreferenceFrozenInputs)]
        train, validation = compile_preference_samples(command, paired, Tokenizer())
        assert train.dataset_sha256 != validation.dataset_sha256
        paired[0].source.write_bytes(b'{"changed":true}\n')
        with pytest.raises(TrainingConflict, match="digest"):
            load_training_frozen_inputs(command, supervisor.directory(command.attempt_id), journal)
    finally:
        journal.close()


def test_v3_staging_rejects_sft_split_and_wrong_analysis_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    from test_training_mixture_worker import frozen_sources

    command, inputs = frozen(tmp_path)
    (tmp_path / "sft").mkdir()
    old_command, old_inputs = frozen_sources(tmp_path / "sft")
    with pytest.raises(TrainingConflict, match="pair inputs"):
        validate_training_frozen_inputs(command, old_inputs[0])
    with pytest.raises(TrainingConflict, match="cannot consume"):
        validate_training_frozen_inputs(old_command, inputs[0])
    altered = replace(
        inputs[0], analysis=inputs[0].analysis.model_copy(update={"runtime_sha256": "f" * 64})
    )
    with pytest.raises(TrainingConflict):
        validate_training_frozen_inputs(command, altered)
    assert isinstance(
        make_frozen_inputs(
            inputs[0].binding, inputs[0].split, inputs[0].analysis, inputs[0].source
        ),
        PreferenceFrozenInputs,
    )
