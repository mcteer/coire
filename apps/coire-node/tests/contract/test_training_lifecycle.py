"""Owned training intent, half-spawn recovery, fences and release-after-proof."""

from __future__ import annotations

import platform
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import (
    TrainingLeaseRenewal,
    TrainingPauseRequest,
    TrainingPrepareRequest,
    TrainingStartRequest,
    TrainingStopRequest,
)
from coire_node.training.checkpoints import CheckpointStore
from coire_node.training.journal import TrainingJournal
from coire_node.training.objectives import SftRuntime
from coire_node.training.supervisor import TrainingSupervisor
from coire_node.training.worker import FrozenInputs

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.mark.asyncio
async def test_shared_training_conversion_admission_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from coire_core.models.acquisition import ReservationRequest
    from coire_node.reservations import ReservationLedger, ReservationRefused
    from coire_node.testing.harness import Agent

    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    agent.settings.node_memory_budget_fraction = 0.9
    monkeypatch.setattr(
        "coire_node.reservations.psutil.virtual_memory", lambda: SimpleNamespace(total=100)
    )
    lock = threading.RLock()
    journal = TrainingJournal(tmp_path / "attempts", node="coire-edge-a", admission_lock=lock)
    training = TrainingSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/v1/bin/python"),
        validate_ready=lambda _: None,
        disk_floor=0,
    )
    ledger = ReservationLedger(
        agent.settings,
        agent.store,
        training.committed_bytes,
        memory_lock=lock,
        additional_held_disk_bytes=training.held_disk_bytes,
    )
    training.memory_available = lambda: 90 - ledger.held_bytes()
    training.disk_available = lambda: (
        10000 - ledger.held_disk_bytes(journal.root, include_external=False)
    )
    command = prepare_command()
    request = ReservationRequest(
        idempotency_key=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        memory_bytes=55,
        disk_bytes=1,
    )
    try:
        await training.prepare(command)
        with pytest.raises(ReservationRefused):
            ledger.hold(request)
        stop_command = TrainingStopRequest.model_validate(
            {**envelope(command), "command_id": uuid.uuid4(), "reason": "cancelled"}
        )
        assert (await training.stop(stop_command)).stopped
        journal.release_after_death(command.attempt_id)
        ledger.hold(request)
        next_command = command.model_copy(
            update={
                "command_id": uuid.uuid4(),
                "attempt_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
                "fence": 2,
            }
        )
        with pytest.raises(TrainingValidationError, match="headroom"):
            await training.prepare(next_command)
        assert training.committed_bytes() == 0 and ledger.held_bytes() == 55
    finally:
        journal.close()
        agent.close()


@pytest.mark.asyncio
async def test_local_watchdog_enforces_expired_lease_and_releases_only_after_death(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from contextlib import suppress

    journal, supervisor, processes, command = setup
    await supervisor.start(start(command))
    with journal.transaction():
        value = journal.get(command.attempt_id)
        value["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        journal.save(value)
    released = asyncio.Event()
    release = journal.release_after_death

    def notify(attempt_id: str) -> bool:
        result = release(attempt_id)
        released.set()
        return cast(bool, result)

    monkeypatch.setattr(journal, "release_after_death", notify)
    task = asyncio.create_task(supervisor.watchdog())
    try:
        await asyncio.wait_for(released.wait(), timeout=2)
        assert processes.state == "stopped"
        assert supervisor.observe(command.attempt_id).reason == "lease_expired"
        assert journal.held_bytes()[1] == 500  # Retained disk never becomes imaginary free space.
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_native_routes_auth_scope_and_disabled_history(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from coire_core.models.node import NetworkPath
    from coire_node.agent import create_app
    from coire_node.testing.harness import TOKEN, Agent

    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    command = prepare_command()
    journal = TrainingJournal(
        tmp_path / "attempts", node=command.node, admission_lock=threading.RLock()
    )
    supervisor = TrainingSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/v1/bin/python"),
        processes=FakeProcesses(),
        validate_ready=lambda _: None,
        memory_available=lambda: 16 * 1024**2,
        disk_available=lambda: 32 * 1024**2,
        disk_floor=0,
    )
    agent.settings.training_enabled = True
    path = f"/node/training/attempts/{command.attempt_id}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        control = create_app(
            agent.settings, agent.collector, listener=NetworkPath.CONTROL, training=supervisor
        )
        with TestClient(control) as client:
            capabilities = client.get("/node/health", headers=headers).json()[
                "training_capabilities"
            ]
            assert capabilities == {
                "spec_versions": [1, 2, 3],
                "evaluation_checkpoint_ack_versions": [1],
            }
            assert (
                client.post(path + "/prepare", json=command.model_dump(mode="json")).status_code
                == 401
            )
            bad = command.model_copy(update={"node": "coire-edge-b"})
            assert (
                client.post(
                    path + "/prepare", headers=headers, json=bad.model_dump(mode="json")
                ).status_code
                == 409
            )
            assert client.post(
                path + "/prepare", headers=headers, json=command.model_dump(mode="json")
            ).json()["ready"]
            assert (
                client.post(
                    path + "/start", headers=headers, json=start(command).model_dump(mode="json")
                ).status_code
                == 200
            )
            agent.settings.training_enabled = False
            assert client.get(path, headers=headers).json()["liveness"] == "running"
            assert client.get(path + "/events?after=-1", headers=headers).status_code == 422
            assert (
                client.post(
                    path + "/prepare", headers=headers, json=command.model_dump(mode="json")
                ).status_code
                == 503
            )
            stop_command = TrainingStopRequest.model_validate(
                {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "cancelled"}
            )
            assert client.post(
                path + "/stop", headers=headers, json=stop_command.model_dump(mode="json")
            ).json()["stopped"]
            assert journal.held_bytes()[0] == 0
            assert client.get(path + "/events", headers=headers).json()["items"] == []
        data = create_app(
            agent.settings, agent.collector, listener=NetworkPath.DATA, training=supervisor
        )
        with TestClient(data) as client:
            assert client.get(path, headers=headers).status_code == 404
    finally:
        journal.close()
        agent.close()


@pytest.mark.asyncio
async def test_native_grant_receipt_is_pending_until_delivery_and_secret_is_not_journaled(
    binding_setup: Any,
) -> None:
    import asyncio

    from pydantic_settings import SettingsConfigDict

    from coire_core.models.training_node import (
        DatasetInputGrant,
        TrainingInputSource,
        TrainingInputsRequest,
    )
    from coire_core.settings import Settings

    journal, supervisor, command, frozen = binding_setup

    class IsolatedSettings(Settings):
        model_config = SettingsConfigDict(secrets_dir=journal.root)

    supervisor.disk_available = lambda: 32 * 1024**2
    supervisor.disk_floor = 0
    secret = "a-private-grant-secret-that-must-not-be-persisted"
    grant = DatasetInputGrant(
        grant_id=uuid.uuid4(),
        node=command.node,
        dataset_id=frozen.binding.dataset_id,
        source_sha256=frozen.binding.source_sha256,
        max_bytes=1024,
        attempt_id=command.attempt_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=25),
        secret=secret,
    )
    request = TrainingInputsRequest(
        **{**envelope(command), "command_id": uuid.uuid4()},
        sources=[
            TrainingInputSource(
                binding=frozen.binding, split=frozen.split, analysis=frozen.analysis, grant=grant
            )
        ],
    )
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delivery(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await finish.wait()

    supervisor._deliver_sources = delivery
    receipt = await supervisor.submit_inputs(request, settings=IsolatedSettings())
    assert not receipt.ready and receipt.reason == "analysis_pending"
    await entered.wait()
    assert secret not in "\n".join(journal.db.iterdump())
    stop_command = TrainingStopRequest.model_validate(
        {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "cancelled"}
    )
    assert (await supervisor.stop(stop_command)).stopped


def frozen_fixture(tmp_path: Path) -> tuple[TrainingPrepareRequest, FrozenInputs]:
    import hashlib
    import json

    from coire_core.models.datasets import (
        DatasetAnalysis,
        DatasetAnalysisBinding,
        DatasetFormat,
        SplitManifest,
        TokenDistribution,
    )
    from coire_core.training_data import normalize_row, split_digest
    from coire_node.training.worker import payload_sha256

    command = prepare_command()
    command.resolved.spec.data.loss_policy = "all_tokens"
    command.resolved.resource_envelope.buffer_bytes = 1024**2
    selected = command.resolved.datasets[0]
    rows = [{"text": f"private synthetic row {index}"} for index in range(6)]
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
    source.chmod(0o600)
    selected.source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    split = SplitManifest(
        dataset_id=selected.dataset_id,
        source_sha256=selected.source_sha256,
        seed=0,
        train_rows=[1, 2, 3, 4],
        validation_rows=[5, 6],
        row_content_sha256=[
            normalize_row(
                row, format=DatasetFormat.TEXT, dataset_id=selected.dataset_id, source_row=index + 1
            ).content_sha256()
            for index, row in enumerate(rows)
        ],
    )
    selected.split_sha256 = split_digest(split)
    binding = DatasetAnalysisBinding(
        dataset_id=selected.dataset_id,
        model_id=command.resolved.spec.model.model_id,
        variant_id=command.resolved.spec.model.variant_id,
        base_manifest_sha256=command.resolved.base_manifest_sha256,
        source_sha256=selected.source_sha256,
        split_sha256=selected.split_sha256,
        format=DatasetFormat.TEXT,
        model_slug="synthetic--base",
    )
    analysis = DatasetAnalysis(
        id=selected.analysis_id,
        dataset_id=selected.dataset_id,
        model_id=binding.model_id,
        variant_id=binding.variant_id,
        state="succeeded",
        row_count=6,
        tokenizer_sha256=command.resolved.tokenizer_sha256,
        template_sha256=command.resolved.template_sha256,
        runtime_sha256=command.resolved.runtime_sha256,
        created_at=datetime.now(UTC),
        tokens=TokenDistribution(
            minimum=3, maximum=3, p50=3, p95=3, histogram=[6], upper_bounds=[4]
        ),
    )
    selected.analysis_sha256 = payload_sha256(analysis)
    return command, FrozenInputs(binding, split, analysis, source)


@pytest.fixture
def binding_setup(
    tmp_path: Path,
) -> Iterator[tuple[TrainingJournal, TrainingSupervisor, TrainingPrepareRequest, FrozenInputs]]:
    command, inputs = frozen_fixture(tmp_path)
    journal = TrainingJournal(
        tmp_path / "journal", node=command.node, admission_lock=threading.RLock()
    )
    journal.prepare(
        command, memory_available=16 * 1024**2, disk_available=32 * 1024**2, disk_floor=0
    )
    supervisor = TrainingSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/v1/bin/python"),
        processes=FakeProcesses(),
        validate_ready=lambda prepared: None,
        store_root=tmp_path / "models",
    )
    yield journal, supervisor, command, inputs
    journal.close()


@pytest.mark.asyncio
async def test_frozen_native_binding_accounts_inputs_and_rechecks_split_rows(
    binding_setup: Any,
) -> None:
    from coire_node.training.worker import compile_samples, load_frozen_inputs

    journal, supervisor, command, inputs = binding_setup
    before = journal.held_bytes()[1]
    await supervisor.bind_inputs(command, inputs, disk_available=32 * 1024**2)
    assert journal.held_bytes()[1] > before
    bound = load_frozen_inputs(command, supervisor.directory(command.attempt_id), journal)
    assert bound.binding == inputs.binding and bound.split == inputs.split

    class Tokenizer:
        has_chat_template = False

        def encode(self, text: str) -> list[int]:
            return [1, 2, 3]

        def apply_chat_template(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: list[dict[str, Any]] | None,
            tokenize: bool,
            add_generation_prompt: bool,
            enable_thinking: bool,
        ) -> list[int]:
            raise AssertionError("Raw text must not use a chat template")

    training, validation = compile_samples(command, bound, Tokenizer())
    assert {example.source_row for example in training.dataset} == {1, 2, 3, 4}
    assert {example.source_row for example in validation.dataset} == {5, 6}
    assert training.dataset_sha256 != validation.dataset_sha256
    bound.source.write_bytes(b'{"text":"changed"}\n')
    with pytest.raises(TrainingConflict, match="digest"):
        load_frozen_inputs(command, supervisor.directory(command.attempt_id), journal)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["split", "analysis", "model", "source", "symlink", "quota", "prepared"]
)
async def test_binding_refuses_changed_inputs_before_any_engine_import(
    binding_setup: Any, change: str
) -> None:
    journal, supervisor, command, inputs = binding_setup
    if change == "split":
        inputs.split.seed = 3
    elif change == "analysis":
        inputs.analysis.runtime_sha256 = "9" * 64
    elif change == "model":
        inputs.binding.variant_id = uuid.uuid4()
    elif change == "source":
        inputs.source.write_bytes(b'{"text":"changed"}\n')
    elif change == "symlink":
        inputs.source.with_suffix(".original").write_bytes(inputs.source.read_bytes())
        inputs.source.unlink()
        inputs.source.symlink_to(inputs.source.with_suffix(".original"))
    elif change == "prepared":
        command = command.model_copy(deep=True)
        command.resolved.spec.optim.updates += 1
    with pytest.raises((TrainingConflict, TrainingValidationError)):
        await supervisor.bind_inputs(
            command, inputs, disk_available=0 if change == "quota" else 32 * 1024**2
        )
    assert journal.get(command.attempt_id).get("input_files") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery", ["valid", "digest", "oversize", "redirect", "compressed", "scope", "origin"]
)
async def test_training_source_grant_has_fixed_origin_bounds_and_no_persisted_secret(
    binding_setup: Any, delivery: str
) -> None:
    import httpx
    from pydantic import SecretStr
    from pydantic_settings import SettingsConfigDict

    from coire_core.models.training_node import DatasetInputGrant
    from coire_core.settings import Settings

    journal, supervisor, command, inputs = binding_setup
    data = inputs.source.read_bytes()
    secret = "grant-secret-" + "x" * 32
    grant = DatasetInputGrant(
        grant_id=uuid.uuid4(),
        node=command.node,
        dataset_id=inputs.binding.dataset_id,
        source_sha256=inputs.binding.source_sha256,
        max_bytes=len(data),
        attempt_id=command.attempt_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=25),
        secret=secret,
    )
    if delivery == "scope":
        grant.attempt_id = "01ARZ3NDEKTSV4RRFFQ69G5FAW"

    class IsolatedSettings(Settings):
        model_config = SettingsConfigDict(secrets_dir=journal.root)

    settings = IsolatedSettings(
        node_name=command.node,
        node_token=SecretStr("node-secret-" + "y" * 32),
        training_input_api_url="http://wrong-origin.invalid"
        if delivery == "origin"
        else "http://coire-core.lab:8180",
    )
    seen: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == settings.core_control_host
        assert request.url.path == f"/api/v1/internal/training/datasets/{grant.dataset_id}/content"
        assert request.headers["X-Coire-Dataset-Grant"] == secret
        if delivery == "redirect":
            return httpx.Response(307, headers={"Location": "http://untrusted.invalid/"})
        if delivery == "compressed":
            return httpx.Response(200, headers={"Content-Encoding": "unsupported"}, content=data)
        return httpx.Response(
            200,
            content=data + b"x"
            if delivery == "oversize"
            else b"wrong"
            if delivery == "digest"
            else data,
        )

    if delivery == "valid":
        await supervisor.download_inputs(
            command,
            grant,
            inputs,
            settings=settings,
            disk_available=32 * 1024**2,
            transport=httpx.MockTransport(serve),
        )
        assert (
            journal.get(command.attempt_id)["input_files"]["source.jsonl"]
            == inputs.binding.source_sha256
        )
        assert secret not in str(journal.records())
        for path in supervisor.directory(command.attempt_id).iterdir():
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
    else:
        with pytest.raises((TrainingConflict, TrainingValidationError, httpx.HTTPStatusError)):
            await supervisor.download_inputs(
                command,
                grant,
                inputs,
                settings=settings,
                disk_available=32 * 1024**2,
                transport=httpx.MockTransport(serve),
            )
        assert journal.get(command.attempt_id).get("input_files") is None
    assert len(seen) == (0 if delivery in {"scope", "origin"} else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "runtime_mismatch", "source_changed"])
async def test_native_cli_consumes_only_frozen_assets_before_bare_execution(
    binding_setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """The entire CLI bootstrap uses CPU fakes; no engine module or weights are loaded."""
    import os
    import sys
    from types import SimpleNamespace

    from coire_node.training import worker
    from coire_node.training.objectives import SftInput

    journal, supervisor, command, inputs = binding_setup
    await supervisor.bind_inputs(command, inputs, disk_available=32 * 1024**2)
    request = start(command)
    await supervisor.start(request)
    # Model the real half-spawn window, before the parent has published PID/create-time.
    with journal.transaction():
        value = journal.get(command.attempt_id)
        value["pid"], value["process_create_time"] = None, None
        journal.save(value)
    monkeypatch.setattr(platform, "node", lambda: "isolated-test-worker")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(os, "getpgrp", os.getpid)
    monkeypatch.setattr(worker, "phys_footprint", lambda pid: 4)

    class Tokenizer:
        has_chat_template = False

        def encode(self, text: str) -> list[int]:
            return [1, 2, 3]

        def apply_chat_template(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: list[dict[str, Any]] | None,
            tokenize: bool,
            add_generation_prompt: bool,
            enable_thinking: bool,
        ) -> list[int]:
            raise AssertionError("Text must not render a chat template")

    seen: list[str] = []

    def tokenizer(path: Path, binding: Any) -> tuple[Any, str, str, str]:
        seen.append("tokenizer")
        assert path == supervisor.store_root.resolve() / inputs.binding.model_slug
        assert binding == inputs.binding
        return (
            Tokenizer(),
            command.resolved.tokenizer_sha256,
            command.resolved.template_sha256,
            "9" * 64 if outcome == "runtime_mismatch" else command.resolved.runtime_sha256,
        )

    def validate(path: Path, parameters: Any, **kwargs: Any) -> SftInput:
        assert path == supervisor.store_root.resolve() / inputs.binding.model_slug
        assert kwargs["expected_manifest_sha256"] == command.resolved.base_manifest_sha256
        return cast(SftInput, SimpleNamespace(root=path))

    def load(source: SftInput, *, seed: int) -> SftRuntime:
        seen.append("weights")
        assert seed == command.resolved.spec.seed
        return cast(SftRuntime, SimpleNamespace(tokenizer=Tokenizer()))

    def run(
        prepared: TrainingPrepareRequest,
        runtime: SftRuntime,
        optimizer: Any,
        training: Any,
        validation: Any,
        **kwargs: Any,
    ) -> int:
        seen.append("train")
        assert prepared == command
        assert {example.source_row for example in training.dataset} == {1, 2, 3, 4}
        assert {example.source_row for example in validation.dataset} == {5, 6}
        assert kwargs["completed_update"] == 0
        assert kwargs["footprint"]() == 4
        assert kwargs["control"]() == "continue"
        return 4

    monkeypatch.setattr(worker, "load_analysis_tokenizer", tokenizer)
    monkeypatch.setattr(worker, "validate_sft_input", validate)
    monkeypatch.setattr(worker, "load_sft_runtime", load)
    monkeypatch.setattr(
        worker,
        "make_optimizer",
        lambda settings: SimpleNamespace(step=SimpleNamespace(item=lambda: 0)),
    )
    monkeypatch.setattr(worker, "run_sft", run)
    if outcome == "source_changed":
        (supervisor.directory(command.attempt_id) / "source.jsonl").write_bytes(
            b'{"text":"changed"}\n'
        )
    value = journal.get(command.attempt_id)
    monkeypatch.setattr(sys, "argv", ["coire_node.training.worker", *supervisor.argv(value)[3:]])
    assert worker.main() == (0 if outcome == "complete" else 1)
    assert seen == (
        ["tokenizer", "weights", "train"]
        if outcome == "complete"
        else ["tokenizer"]
        if outcome == "runtime_mismatch"
        else []
    )
    if outcome != "complete":
        assert journal.events(command.attempt_id).items[-1].payload.kind == "failure"


@pytest.mark.asyncio
async def test_private_worker_event_journal_replay_cursor_and_fencing(binding_setup: Any) -> None:
    from coire_core.models.training_node import NodeControlPayload, NodeTrainingEvent

    journal, supervisor, command, _ = binding_setup
    await supervisor.start(start(command))
    event = NodeTrainingEvent(
        sequence=1,
        job_id=command.job_id,
        attempt_id=command.attempt_id,
        fence=command.fence,
        update=0,
        recorded_at=datetime.now(UTC),
        payload=NodeControlPayload(kind="pause_requested", reason="admin_pause"),
    )
    journal.append_event(event)
    journal.append_event(event)
    assert journal.events(command.attempt_id).items == [event]
    assert journal.events(command.attempt_id, 1).items == []
    assert journal.next_sequence(command.attempt_id) == 2
    with pytest.raises(TrainingConflict, match="immutable"):
        journal.append_event(event.model_copy(update={"update": 1}))
    with pytest.raises(TrainingConflict, match="contiguous"):
        journal.append_event(event.model_copy(update={"sequence": 3}))
    with pytest.raises(TrainingConflict, match="authority"):
        journal.append_event(event.model_copy(update={"sequence": 2, "fence": 2}))


@pytest.mark.asyncio
async def test_cancel_waits_for_owned_staging_thread_before_stop_proof(
    binding_setup: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    journal, supervisor, command, inputs = binding_setup
    entered, proceed = threading.Event(), threading.Event()
    original = supervisor._bind_inputs

    def stalled(
        prepared: TrainingPrepareRequest, frozen: FrozenInputs, disk_available: int
    ) -> None:
        entered.set()
        assert proceed.wait(timeout=5)
        original(prepared, frozen, disk_available)

    monkeypatch.setattr(supervisor, "_bind_inputs", stalled)
    preparing = asyncio.create_task(
        supervisor.bind_inputs(command, inputs, disk_available=32 * 1024**2)
    )
    stopping: asyncio.Task[Any] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        stop = TrainingStopRequest.model_validate(
            {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "cancelled"}
        )
        stopping = asyncio.create_task(supervisor.stop(stop))
        await asyncio.sleep(0.02)
        assert not stopping.done()
        with pytest.raises(TrainingConflict):
            journal.release_after_death(command.attempt_id)
    finally:
        proceed.set()
    assert stopping is not None and (await stopping).stopped
    with pytest.raises(TrainingConflict):
        await preparing
    assert journal.get(command.attempt_id).get("input_files") is None


def test_native_cli_refuses_core_before_opening_any_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from coire_node.training import worker

    monkeypatch.setattr(platform, "node", lambda: "coire-core")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--attempt",
            JOB,
            "--owner",
            str(uuid.uuid4()),
            "--state-root",
            "/does-not-exist",
            "--store-root",
            "/does-not-exist",
            "--artifact-root",
            "/does-not-exist",
        ],
    )
    with pytest.raises(SystemExit) as failure:
        worker.main()
    assert failure.value.code == 2


@pytest.mark.asyncio
async def test_cancel_closes_scoped_source_stream_before_releasing_preparation(
    binding_setup: Any,
) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    import httpx
    from pydantic import SecretStr
    from pydantic_settings import SettingsConfigDict

    from coire_core.models.training_node import DatasetInputGrant
    from coire_core.settings import Settings

    journal, supervisor, command, inputs = binding_setup
    entered, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self) -> None:
            closed.set()

    class IsolatedSettings(Settings):
        model_config = SettingsConfigDict(secrets_dir=journal.root)

    settings = IsolatedSettings(node_token=SecretStr("node-token-" + "x" * 32))
    grant = DatasetInputGrant(
        grant_id=uuid.uuid4(),
        node=command.node,
        dataset_id=inputs.binding.dataset_id,
        source_sha256=inputs.binding.source_sha256,
        max_bytes=inputs.source.stat().st_size,
        attempt_id=command.attempt_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=25),
        secret="x" * 32,
    )
    preparing = asyncio.create_task(
        supervisor.download_inputs(
            command,
            grant,
            inputs,
            settings=settings,
            disk_available=32 * 1024**2,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    stop = TrainingStopRequest.model_validate(
        {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "cancelled"}
    )
    assert (await asyncio.wait_for(supervisor.stop(stop), timeout=2)).stopped
    with pytest.raises(asyncio.CancelledError):
        await preparing
    assert closed.is_set()
    assert journal.get(command.attempt_id).get("input_files") is None
    assert journal.release_after_death(command.attempt_id)


def prepare_command() -> TrainingPrepareRequest:
    model, variant, dataset = [str(uuid.uuid4()) for _ in range(3)]
    return TrainingPrepareRequest.model_validate(
        {
            "command_id": str(uuid.uuid4()),
            "job_id": JOB,
            "attempt_id": JOB,
            "fence": 1,
            "request_sha256": "a" * 64,
            "node": "coire-edge-a",
            "rank": 0,
            "world_size": 1,
            "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=25)).isoformat(),
            "reservation_id": str(uuid.uuid4()),
            "disk_reservation_id": str(uuid.uuid4()),
            "resolved": {
                "spec": {
                    "model": {"model_id": model, "variant_id": variant},
                    "data": {
                        "train": {
                            "datasets": [
                                {
                                    "dataset_id": dataset,
                                    "sample_count": 4,
                                    "mixture_proportion": 1.0,
                                }
                            ],
                            "epoch_samples": 4,
                        },
                        "validation": {"dataset_ids": [dataset]},
                    },
                    "parameterization": {"target_modules": ["self_attn.q_proj"]},
                    "optim": {"updates": 4},
                    "output": {"adapter_slug": "test-lifecycle"},
                },
                "base_manifest_sha256": "b" * 64,
                "datasets": [
                    {
                        "dataset_id": dataset,
                        "analysis_id": str(uuid.uuid4()),
                        "source_sha256": "c" * 64,
                        "split_sha256": "d" * 64,
                        "analysis_sha256": "e" * 64,
                    }
                ],
                "tokenizer_sha256": "f" * 64,
                "template_sha256": "a" * 64,
                "enable_thinking": False,
                "runtime_sha256": "b" * 64,
                "worker_version": "1",
                "resource_envelope": {
                    "weight_bytes": 10,
                    "adapter_bytes": 2,
                    "optimizer_bytes": 4,
                    "activation_bytes": 8,
                    "buffer_bytes": 2,
                    "safety_bytes": 10,
                    "checkpoint_bytes": 100,
                    "evidence_sha256": "c" * 64,
                },
            },
        }
    )


def envelope(command: TrainingPrepareRequest) -> dict[str, Any]:
    return command.model_dump(
        mode="json",
        exclude={
            "resolved",
            "reservation_id",
            "disk_reservation_id",
            "resume_manifest_sha256",
            "resume_checkpoint_id",
            "collective",
        },
    )


class FakeProcesses:
    def __init__(self) -> None:
        self.spawn_count = 0
        self.state = "running"
        self.half_spawn = False
        self.argv: list[str] = []
        self.env: dict[str, str] = {}

    def spawn(self, argv: list[str], env: dict[str, str]) -> tuple[int, float]:
        self.spawn_count += 1
        self.argv, self.env = argv, env
        if self.half_spawn:
            raise RuntimeError("agent died after Popen, before PID save")
        return 1234, 100.0

    def discover(self, argv: list[str]) -> tuple[str, int | None, float | None]:
        assert argv == self.argv
        return ("running", 1234, 100.0) if self.state == "running" else ("unknown", None, None)

    def observe(self, argv: list[str], pid: int, created: float) -> str:
        assert argv == self.argv and (pid, created) == (1234, 100.0)
        return self.state

    def stop(self, argv: list[str], pid: int, created: float, deadline: float) -> str:
        if self.state == "running":
            self.state = "stopped"
        return self.observe(argv, pid, created)


@pytest.fixture
def setup(
    tmp_path: Path,
) -> Iterator[tuple[TrainingJournal, TrainingSupervisor, FakeProcesses, TrainingPrepareRequest]]:
    journal = TrainingJournal(
        tmp_path / "journal", node="coire-edge-a", admission_lock=threading.RLock()
    )
    processes = FakeProcesses()
    supervisor = TrainingSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/v1/bin/python"),
        validate_ready=lambda command: None,
        processes=processes,
    )
    command = prepare_command()
    journal.prepare(command, memory_available=1000, disk_available=1000, disk_floor=0)
    yield journal, supervisor, processes, command
    journal.close()


def start(command: TrainingPrepareRequest) -> TrainingStartRequest:
    return TrainingStartRequest.model_validate(
        {
            **envelope(command),
            "command_id": str(uuid.uuid4()),
            "prepared_command_id": str(command.command_id),
            "spawn_nonce": str(uuid.uuid4()),
        }
    )


def test_prepare_immutable_replay_and_aggregate_hold(setup: Any) -> None:
    journal, _, _, command = setup
    assert journal.held_bytes() == (36, 500)
    journal.prepare(command, memory_available=0, disk_available=0, disk_floor=0)
    changed = command.model_copy(update={"request_sha256": "f" * 64})
    with pytest.raises(TrainingConflict, match="changed"):
        journal.prepare(changed, memory_available=1000, disk_available=1000, disk_floor=0)
    with pytest.raises(TrainingConflict, match="Unknown"):
        journal.release_after_death(command.attempt_id)


@pytest.mark.asyncio
async def test_start_replay_owned_argv_offline_and_stop_proof(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal, supervisor, processes, command = setup
    monkeypatch.setenv("HF_TOKEN", "must-not-inherit")
    request = start(command)
    receipt = await supervisor.start(request)
    assert await supervisor.start(request) == receipt
    assert processes.spawn_count == 1
    assert processes.argv[:3] == [
        "/opt/coire/envs/v1/bin/python",
        "-m",
        "coire_node.training.worker",
    ]
    assert "HF_TOKEN" not in processes.env and processes.env["HF_HUB_OFFLINE"] == "1"
    stop = TrainingStopRequest.model_validate(
        {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "cancelled"}
    )
    assert (await supervisor.stop(stop)).stopped
    assert journal.release_after_death(command.attempt_id)
    assert not journal.release_after_death(command.attempt_id)
    # Artifacts outlive compute; death does not free their aggregate disk hold.
    assert journal.held_bytes() == (0, 500)


async def test_training_worker_receives_only_configured_telemetry_endpoint(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal, supervisor, processes, command = setup
    supervisor.otlp_endpoint = "http://coire-core.lab:4317"
    monkeypatch.setenv("OTLP_ENDPOINT", "http://untrusted.invalid:4317")
    monkeypatch.setenv("HF_TOKEN", "must-not-inherit")
    monkeypatch.setenv("NODE_CONTROL_TOKEN", "must-not-inherit")
    await supervisor.start(start(command))
    assert processes.env["OTLP_ENDPOINT"] == "http://coire-core.lab:4317"
    assert "HF_TOKEN" not in processes.env and "NODE_CONTROL_TOKEN" not in processes.env


@pytest.mark.asyncio
async def test_half_spawn_re_adopts_without_duplicate(setup: Any) -> None:
    journal, supervisor, processes, command = setup
    request = start(command)
    processes.half_spawn = True
    with pytest.raises(RuntimeError):
        await supervisor.start(request)
    assert journal.get(command.attempt_id)["pid"] is None
    assert supervisor.observe(command.attempt_id).liveness == "running"
    assert (await supervisor.start(request)).pid == 1234
    assert processes.spawn_count == 1


@pytest.mark.asyncio
async def test_pid_reuse_unknown_never_killed_or_released(setup: Any) -> None:
    journal, supervisor, processes, command = setup
    await supervisor.start(start(command))
    processes.state = "unknown"
    stop = TrainingStopRequest.model_validate(
        {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "cancelled"}
    )
    assert not (await supervisor.stop(stop)).stopped
    with pytest.raises(TrainingConflict):
        journal.release_after_death(command.attempt_id)
    assert journal.held_bytes() == (36, 500)


@pytest.mark.asyncio
async def test_pause_deadline_is_not_extended_and_renew_cannot_resurrect(setup: Any) -> None:
    journal, supervisor, _, command = setup
    await supervisor.start(start(command))
    pause = TrainingPauseRequest.model_validate(
        {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "admin_pause"}
    )
    await supervisor.pause(pause)
    deadline = journal.get(command.attempt_id)["pause_deadline"]
    await supervisor.pause(pause.model_copy(update={"command_id": uuid.uuid4()}))
    assert journal.get(command.attempt_id)["pause_deadline"] == deadline
    with journal.transaction():
        value = journal.get(command.attempt_id)
        value["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        journal.save(value)
    renewal = TrainingLeaseRenewal.model_validate(
        {
            **envelope(command),
            "command_id": str(uuid.uuid4()),
            "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=29)).isoformat(),
        }
    )
    with pytest.raises(TrainingConflict, match="resurrect"):
        await supervisor.renew(renewal)


@pytest.mark.parametrize("change", ["node", "fence", "lease", "disk", "memory"])
def test_prepare_refuses_wrong_scope_expiry_and_envelope(tmp_path: Path, change: str) -> None:
    journal = TrainingJournal(
        tmp_path / "journal", node="coire-edge-a", admission_lock=threading.RLock()
    )
    command = prepare_command()
    if change == "node":
        command = command.model_copy(update={"node": "coire-edge-b"})
    if change == "lease":
        command = command.model_copy(
            update={"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
    if change == "fence":
        journal.prepare(command, memory_available=1000, disk_available=1000, disk_floor=0)
        with journal.transaction():
            journal.db.execute("UPDATE attempts SET fence=2")
    with pytest.raises((TrainingConflict, TrainingValidationError)):
        journal.prepare(
            command,
            memory_available=0 if change == "memory" else 1000,
            disk_available=0 if change == "disk" else 1000,
            disk_floor=0,
        )
    journal.close()


def test_journal_restart_preserves_counted_unknown_hold(setup: Any) -> None:
    journal, _, _, command = setup
    other = TrainingJournal(journal.root, node=journal.node, admission_lock=journal.lock)
    assert other.get(command.attempt_id) == journal.get(command.attempt_id)
    assert other.held_bytes() == (36, 500)
    other.close()


def test_native_session_pid_identity_and_bounded_group_stop() -> None:
    import sys
    import time

    from coire_node.training.supervisor import NativeProcesses

    processes = NativeProcesses()
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    pid, created = processes.spawn(argv, {})
    try:
        assert processes.observe(argv, pid, created) == "running"
        assert processes.observe(argv, pid, created + 1) == "unknown"
        assert processes.stop(argv, pid, created + 1, time.monotonic() + 1) == "unknown"
        assert processes.observe(argv, pid, created) == "running"
        began = time.monotonic()
        assert processes.stop(argv, pid, created, began + 5) == "stopped"
        assert time.monotonic() - began < 5
    finally:
        child = processes.children[pid]
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.asyncio
async def test_owner_only_control_rejects_changed_command_and_wrong_attempt(setup: Any) -> None:
    from coire_node.store import write_atomic
    from coire_node.training.worker import PrivateControl

    journal, supervisor, _, command = setup
    request = start(command)
    await supervisor.start(request)
    directory = supervisor.directory(command.attempt_id)
    channel = PrivateControl(directory, command, str(request.spawn_nonce))
    assert channel.poll() == "continue"
    pause = TrainingPauseRequest.model_validate(
        {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "admin_pause"}
    )
    write_atomic(directory / "pause.json", pause.model_dump_json().encode())
    assert channel.poll() == "pause"
    altered = pause.model_copy(update={"reason": "memory_breach"})
    write_atomic(directory / "pause.json", altered.model_dump_json().encode())
    with pytest.raises(TrainingConflict, match="changed"):
        channel.poll()
    altered = pause.model_copy(update={"command_id": uuid.uuid4(), "fence": 2})
    write_atomic(directory / "pause.json", altered.model_dump_json().encode())
    with pytest.raises(TrainingConflict, match="scope"):
        channel.poll()
    journal.root.chmod(0o700)


@pytest.mark.asyncio
async def test_reconcile_matches_nonce_and_identity_without_releasing(setup: Any) -> None:
    from coire_core.models.training_node import TrainingReconcileRequest

    journal, supervisor, processes, command = setup
    request = start(command)
    await supervisor.start(request)
    expected = {
        "attempt_id": command.attempt_id,
        "fence": 1,
        "spawn_nonce": str(request.spawn_nonce),
        "pid": 1234,
        "process_create_time": 100.0,
    }
    result = await supervisor.reconcile(
        TrainingReconcileRequest.model_validate({"expected": [expected]})
    )
    assert [item.attempt_id for item in result.adopted] == [command.attempt_id]
    expected["spawn_nonce"] = str(uuid.uuid4())
    assert (
        await supervisor.reconcile(
            TrainingReconcileRequest.model_validate({"expected": [expected]})
        )
    ).unknown == [command.attempt_id]
    assert processes.spawn_count == 1 and journal.held_bytes() == (36, 500)


@pytest.mark.asyncio
async def test_slow_asset_validation_does_not_block_cancel_or_start_after_stop(setup: Any) -> None:
    import asyncio

    _, supervisor, processes, command = setup
    validating, proceed = threading.Event(), threading.Event()

    def validate(prepared: TrainingPrepareRequest) -> None:
        validating.set()
        assert proceed.wait(timeout=5)

    supervisor.validate_ready = validate
    pending = asyncio.create_task(supervisor.start(start(command)))
    try:
        assert await asyncio.to_thread(validating.wait, 2)
        stop = TrainingStopRequest.model_validate(
            {**envelope(command), "command_id": str(uuid.uuid4()), "reason": "cancelled"}
        )
        assert (await asyncio.wait_for(supervisor.stop(stop), timeout=1)).stopped
    finally:
        proceed.set()
    with pytest.raises(TrainingConflict, match="Stopped"):
        await pending
    assert processes.spawn_count == 0


@pytest.mark.parametrize(
    "outcome",
    [
        "complete",
        "pause",
        "evaluation_pause",
        "no_commit",
        "stale_commit",
        "nonfinite",
        "bad_counter",
        "bad_eval",
    ],
)
@pytest.mark.parametrize("mixture", [False, True], ids=["single", "mixture"])
def test_unchanged_trainer_hook_boundaries_and_independent_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    mixture: bool,
) -> None:
    """Fake the engine boundary, not the sampler/state/event/worker implementation."""
    import sys
    from types import SimpleNamespace

    from coire_core.models.datasets import TokenizedTrainingExample
    from coire_node.training import worker
    from coire_node.training.datasets import index_training_source
    from coire_node.training.sampler import MixtureSampler, SingleSourceSampler, TrainingSampler

    # All engine modules below are inert fakes. No model or Metal work occurs here.
    monkeypatch.setattr(platform, "node", lambda: "test-offline-worker")
    prepared = prepare_command()
    prepared.resolved.spec.optim.accumulation_steps = 2
    prepared.resolved.spec.output.checkpoint_every_updates = 2
    prepared.resolved.spec.eval.loss_every_updates = 2
    if outcome == "evaluation_pause":
        from coire_core.evaluation_suites import template
        from coire_core.models.evaluation import EvaluationSuite, EvaluationWorkload
        from coire_core.models.training import parse_resolved_training_spec

        frozen_workload = EvaluationWorkload.model_validate_json(
            (
                Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"
            ).read_bytes()
        )
        frozen = frozen_workload.target.model_dump(mode="json")
        resolved = prepared.resolved.model_dump(mode="json")
        schedule = {"suite_id": "checkpoint-task", "suite_version": 1, "checkpoint_updates": [2]}
        resolved["spec"]["schema_version"] = 2
        resolved["spec"]["eval"]["suites"] = [schedule]
        frozen["target"].update(
            model_id=resolved["spec"]["model"]["model_id"],
            variant_id=resolved["spec"]["model"]["variant_id"],
            base_manifest_sha256=resolved["base_manifest_sha256"],
        )
        frozen["runtime"].update(
            tokenizer_sha256=resolved["tokenizer_sha256"],
            template_sha256=resolved["template_sha256"],
        )
        frozen["public_selector"] = resolved["spec"]["model"]["model_id"]
        resolved["evaluation_base"] = frozen
        suite = EvaluationSuite(
            suite_id="checkpoint-task",
            version=1,
            template=template("task-coding-instructions"),
            generation=frozen_workload.suite.generation,
            timeout_seconds=900,
            content_sha256="a" * 64,
            registered_at=datetime.now(UTC),
        )
        resolved["evaluations"] = [{"schedule": schedule, "suite": suite.model_dump(mode="json")}]
        prepared.resolved = parse_resolved_training_spec(resolved)
    examples = [
        TokenizedTrainingExample(
            source_row=i + 1,
            content_sha256="c" * 64,
            tokens=[1, 2, 3],
            target_mask=[False, True, True],
            target_start=1,
        )
        for i in range(4)
    ]

    def sampler(digest: str) -> TrainingSampler:
        if mixture:
            return MixtureSampler(
                [
                    index_training_source(
                        dataset_id=uuid.UUID(int=index + 1),
                        source_sha256=f"{index + 1:064x}",
                        split_sha256=f"{index + 10:064x}",
                        rows=(1, 2, 3, 4),
                        cache={example.source_row: example for example in examples},
                        quota=quota,
                    )
                    for index, quota in enumerate((3, 2))
                ],
                mixture_sha256=digest * 64,
                batch_size=1,
                seed=7,
                max_sequence_length=2048,
            )
        return SingleSourceSampler(
            examples, dataset_sha256=digest * 64, batch_size=1, seed=7, max_sequence_length=2048
        )

    training, validation = sampler("a"), sampler("b")
    validation_before = validation.snapshot()
    step = [0]
    optimizer = SimpleNamespace(
        step=SimpleNamespace(item=lambda: step[0]),
        learning_rate=SimpleNamespace(item=lambda: 0.01),
        state={"step": 0},
    )
    runtime = SimpleNamespace(
        model=SimpleNamespace(train=lambda: None, trainable_parameters=lambda: {"lora_a": 1}),
        trainable_keys={"lora_a"},
        parameters=lambda: {"lora_a": 1},
    )
    rng = [(1, 2)]
    monkeypatch.setattr(worker, "capture_mlx_rng_key", lambda: rng[0])
    monkeypatch.setattr(worker, "restore_mlx_rng_key", lambda key: rng.__setitem__(0, key))
    mx = SimpleNamespace(
        array=lambda value, **kwargs: value,
        int32="i32",
        bool_="bool",
        eval=lambda *args: None,
        get_peak_memory=lambda: 123,
    )
    calls: list[str] = []

    def evaluate(model: Any, dataset: Any, **kwargs: Any) -> float:
        calls.append("evaluate")
        iterator = kwargs["iterate_batches"](
            comm_group=SimpleNamespace(size=lambda: 1, rank=lambda: 0)
        )
        next(iterator)
        rng[0] = (99, 100)
        return float("nan") if outcome == "bad_eval" else 0.5

    def train(model: Any, optim: Any, dataset: Any, **kwargs: Any) -> None:
        args = kwargs["args"]
        assert args.iters == 8 and args.grad_accumulation_steps == 2
        assert args.steps_per_report == 2 and not args.grad_checkpoint
        assert "val_dataset" not in kwargs
        iterator = kwargs["iterate_batches"](
            comm_group=SimpleNamespace(size=lambda: 1, rank=lambda: 0)
        )
        for iteration in range(1, 9):
            next(iterator)
            if iteration % 2 == 0:
                step[0] += 1
                if outcome == "bad_counter":
                    step[0] += 1
                kwargs["training_callback"].on_train_loss_report(
                    {
                        "iteration": iteration,
                        "train_loss": float("nan") if outcome == "nonfinite" else 1.0,
                    }
                )

    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mx))
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setitem(
        sys.modules, "mlx_lm.tuner.callbacks", SimpleNamespace(TrainingCallback=object)
    )
    monkeypatch.setitem(
        sys.modules,
        "mlx_lm.tuner.trainer",
        SimpleNamespace(
            TrainingArgs=lambda **kwargs: SimpleNamespace(**kwargs), train=train, evaluate=evaluate
        ),
    )
    manifests: list[Any] = []

    def save(state: Any, adapter: Any, optimizer_state: Any) -> Any:
        from coire_core.models.training_node import TrainingArtifactManifest

        assert state.completed_update == step[0]
        assert state.mlx_rng_key == (1, 2)
        assert state.sampler == training.snapshot()
        manifest = TrainingArtifactManifest.model_validate(
            {
                "artifact_id": str(uuid.uuid4()),
                "kind": "checkpoint",
                "job_id": JOB,
                "attempt_id": JOB,
                "fence": 1,
                "update": step[0],
                "world_size": 1,
                "runtime_sha256": state.runtime_sha256,
                "resolved_spec_sha256": state.resolved_spec_sha256,
                "files": [
                    {"id": name, "name": name + ".json", "bytes": 1, "sha256": "c" * 64}
                    for name in ["adapter", "optimizer", "state"]
                ],
                "total_bytes": 3,
                "ranks": [
                    {
                        "rank": 0,
                        "update": step[0],
                        "adapter_file_id": "adapter",
                        "optimizer_file_id": "optimizer",
                        "state_file_id": "state",
                        "adapter_tensors": [{"key": "lora_a", "shape": [1], "dtype": "float32"}],
                        "optimizer_tensors": [{"key": "step", "shape": [], "dtype": "uint32"}],
                    }
                ],
            }
        )
        manifests.append(manifest)
        return manifest

    events: list[Any] = []

    def commit(manifest: Any) -> Any:
        from coire_core.models.training_node import CheckpointCommitAcknowledgement

        if outcome == "no_commit":
            return None
        if outcome == "evaluation_pause":
            from coire_core.models.training_node import CheckpointCommitAcknowledgementV2

            return CheckpointCommitAcknowledgementV2.model_validate(
                {
                    **envelope(prepared),
                    "schema_version": 2,
                    "command_id": str(uuid.uuid4()),
                    "checkpoint_id": str(manifest.artifact_id),
                    "manifest_sha256": manifest.canonical_sha256(),
                    "update": manifest.update,
                    "committed_update": manifest.update,
                    "job_version": 2,
                    "evaluation_pause": {
                        "trigger_id": str(uuid.uuid4()),
                        "pause_command_id": str(uuid.uuid4()),
                    },
                }
            )
        return CheckpointCommitAcknowledgement.model_validate(
            {
                **envelope(prepared),
                "command_id": str(uuid.uuid4()),
                "checkpoint_id": str(manifest.artifact_id),
                "manifest_sha256": manifest.canonical_sha256(),
                "update": manifest.update,
                "fence": 2 if outcome == "stale_commit" else prepared.fence,
            }
        )

    def execute() -> int:
        return worker.run_sft(
            prepared,
            cast(SftRuntime, runtime),
            optimizer,
            training,
            validation,
            store=cast(CheckpointStore, SimpleNamespace(save=save)),
            scratch=tmp_path / "scratch",
            emit=events.append,
            commit=commit,
            control=lambda: "pause" if outcome == "pause" else "continue",
            footprint=lambda: 456,
        )

    if outcome == "complete":
        assert execute() == 4
        assert [item.update for item in manifests] == [2, 4]
        assert calls == ["evaluate", "evaluate"]
        assert validation.snapshot() == validation_before and rng[0] == (1, 2)
        assert [item.sequence for item in events] == list(range(1, len(events) + 1))
        assert all(
            item.payload.metric.footprint_bytes == 456
            for item in events
            if item.payload.kind == "progress"
        )
    elif outcome in {"pause", "evaluation_pause"}:
        with pytest.raises(worker.PausedAtCheckpoint) as stopped:
            execute()
        assert step[0] == (2 if outcome == "evaluation_pause" else 1) and len(manifests) == 1
        if outcome == "evaluation_pause":
            assert stopped.value.reason == "evaluation_pending" and manifests[0].update == 2
    else:
        with pytest.raises((TrainingConflict, TrainingValidationError)):
            execute()
        if outcome in {"nonfinite", "bad_counter", "bad_eval"}:
            assert manifests == []
        if outcome == "bad_eval":
            assert rng[0] == (1, 2) and validation.snapshot() == validation_before
