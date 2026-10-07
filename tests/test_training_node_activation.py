"""Operator activation preserves installed service policy, images and credentials."""

import importlib.util
from pathlib import Path

import pytest


def test_activation_rejects_legacy_runtime_and_accepts_dedicated_identity(tmp_path: Path) -> None:
    path = Path(__file__).parents[1] / "deploy/cluster/scripts/activate-training-node.py"
    spec = importlib.util.spec_from_file_location("training_node_activation_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prefix = tmp_path / "coire"
    runtime = prefix / "python/cpython-version/bin"
    runtime.mkdir(parents=True)
    legacy = runtime / "python3.13"
    legacy.write_text("legacy")
    dedicated = runtime / "coire-node-python"
    dedicated.write_text("dedicated")
    candidate = prefix / "envs/candidate"
    (candidate / "bin").mkdir(parents=True)
    entry = candidate / "bin/python3"
    entry.symlink_to(legacy)
    with pytest.raises(ValueError, match="dedicated runtime"):
        module.validate_candidate_runtime(candidate, prefix=prefix)
    entry.unlink()
    entry.symlink_to(dedicated)
    assert module.validate_candidate_runtime(candidate, prefix=prefix) == dedicated
    outside = tmp_path / "coire-node-python"
    outside.write_text("outside")
    entry.unlink()
    entry.symlink_to(outside)
    with pytest.raises(ValueError, match="dedicated runtime"):
        module.validate_candidate_runtime(candidate, prefix=prefix)


def test_operator_activation_preserves_all_unrelated_plist_settings() -> None:
    path = Path(__file__).parents[1] / "deploy/cluster/scripts/activate-training-node.py"
    spec = importlib.util.spec_from_file_location("training_node_activation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    environment = {
        "NODE_NAME": "coire-edge-a",
        "RUN_AGENT_IMAGE": "existing@sha256:abc",
        "FAILOVER_FRONTEND_IMAGE": "existing-failover@sha256:def",
        "NODE_TOKEN_FILE": "/protected/existing",
    }
    original: dict[str, object] = {
        "Label": "com.coire.node",
        "AbandonProcessGroup": True,
        "ProgramArguments": ["/opt/coire/envs/current/bin/python3", "-m", "coire_node"],
        "UserName": "mcteer",
        "KeepAlive": True,
        "ProcessType": "Standard",
        "EnvironmentVariables": environment,
    }
    proposed = module.replacement(original, "coire-edge-a")
    assert {key: value for key, value in proposed.items() if key != "EnvironmentVariables"} == {
        key: value for key, value in original.items() if key != "EnvironmentVariables"
    }
    assert proposed["EnvironmentVariables"] == {
        **environment,
        "TRAINING_ENABLED": "true",
        "TRAINING_INPUT_API_URL": "http://coire-core.lab:8180",
    }
    assert "TRAINING_ENABLED" not in environment
    with pytest.raises(ValueError, match="identity"):
        module.replacement(original, "coire-edge-b")
    with pytest.raises(ValueError, match="survival"):
        module.replacement({**original, "AbandonProcessGroup": False}, "coire-edge-a")
    agent = "coire-agent@sha256:" + "a" * 64
    relay = "coire-run-relay@sha256:" + "b" * 64
    configured = module.replacement(original, "coire-edge-a", agent_image=agent, relay_image=relay)
    assert configured["EnvironmentVariables"] == {
        **proposed["EnvironmentVariables"],
        "RUN_AGENT_IMAGE": agent,
        "RUN_RELAY_IMAGE": relay,
    }
    assert original["EnvironmentVariables"] == environment
    with pytest.raises(ValueError, match="both nonempty"):
        module.replacement(original, "coire-edge-a", agent_image=agent)
    with pytest.raises(ValueError, match="digest-pinned"):
        module.replacement(
            original, "coire-edge-a", agent_image="coire-agent:latest", relay_image=relay
        )
