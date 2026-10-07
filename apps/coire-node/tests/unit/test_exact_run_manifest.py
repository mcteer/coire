from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from coire_core.models.adapters import InferenceTarget
from coire_core.models.harness import ProfileName
from coire_core.models.runs import RunContainerCreate, RunLimits
from coire_core.settings import Settings
from coire_node.runs import RunManager, RunRuntimeError


def manifest() -> RunContainerCreate:
    target = InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        adapter_id=uuid.uuid4(),
        base_manifest_sha256="a" * 64,
        adapter_manifest_sha256="b" * 64,
    )
    return RunContainerCreate(
        run_id=uuid.uuid4(),
        profile=ProfileName.GENERAL,
        model_id=target.model_id,
        variant_id=target.variant_id,
        target=target,
        public_selector=f"{target.model_id}@trained",
        image=f"ghcr.io/coire/agent@sha256:{'a' * 64}",
        argv=["-m", "coire_agent"],
        workspace_ref="safe",
        run_token="r" * 48,
        gateway_url="http://coire-core.lab/v1",
        limits=RunLimits(),
    )


def test_node_environment_carries_exact_subject_and_public_selector(tmp_path: Path) -> None:
    command = manifest()
    (tmp_path / "safe").mkdir()
    manager = RunManager(
        Settings(run_workspace_root=str(tmp_path), run_agent_image=command.image), Mock()
    )
    payload = manager.create_payload(command, "run-network")
    env = dict(value.split("=", 1) for value in payload["Env"])
    assert InferenceTarget.model_validate_json(env["COIRE_INFERENCE_TARGET"]) == command.target
    assert env["COIRE_PUBLIC_SELECTOR"] == str(command.public_selector)
    assert env["COIRE_VERIFIED_VARIANT_ID"] == str(command.variant_id)


@pytest.mark.parametrize(
    "mutation",
    ["parent", "variant", "missing_selector", "base_selector", "selector_parent", "missing_target"],
)
def test_node_rejects_mismatched_exact_manifest(tmp_path: Path, mutation: str) -> None:
    command = manifest()
    replacements: dict[str, dict[str, object]] = {
        "parent": {"model_id": uuid.uuid4()},
        "variant": {"variant_id": uuid.uuid4()},
        "missing_selector": {"public_selector": None},
        "base_selector": {"public_selector": command.model_id},
        "selector_parent": {"public_selector": f"{uuid.uuid4()}@trained"},
        "missing_target": {"target": None},
    }
    changed = command.model_copy(update=replacements[mutation])
    manager = RunManager(
        Settings(run_workspace_root=str(tmp_path), run_agent_image=command.image), Mock()
    )
    with pytest.raises(RunRuntimeError) as refused:
        manager.create_payload(changed, "run-network")
    assert refused.value.code == "run_target_invalid"
