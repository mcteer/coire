"""Upgrade serving proof requires actual generation and positive owned cleanup."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from upgrade_fixtures import plan

from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings


def test_core_cannot_start_upgrade_smoke() -> None:
    from coire_node.upgrade_smoke import run_node_smoke

    client = Mock()
    with pytest.raises(UpgradeUnavailable):
        run_node_smoke(
            Settings(node_name="core"),
            plan(),
            "a" * 64,
            datetime.now(UTC) + timedelta(seconds=120),
            client,
        )
    client.post.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "short_generation",
        "unreadable_cleanup",
        "changed_fence",
        "wrong_model",
        "changed_asset",
        "missing_asset",
        "engine_readiness",
    ],
)
def test_generation_proof_requires_positive_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import json

    import psutil
    from test_upgrade_runtime_selection import prepared

    from coire_core.models.engine import EngineState, EngineStatus
    from coire_core.models.upgrades import NativeSmokeProgress, UpgradeOwnerInventory
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node import upgrade_smoke
    from coire_node.store import Store
    from coire_node.upgrade_inventory import upgrade_smoke_entries

    envs, _, runtime = prepared(tmp_path)
    node_store = Store(tmp_path / "models")
    model = node_store.path_for("owner--tiny")
    model.mkdir(parents=True)
    (model / "weights").write_bytes(b"tiny-fixture")
    manifest = node_store.hash_tree("owner--tiny", repo_id="owner/tiny", revision="a" * 40)
    node_store.write_manifest(manifest)
    value = plan()
    value = value.model_copy(update={"model_manifest_sha256": manifest.sha256()})
    source = value.source.manifest.model_copy(
        update={"environment_sha256": runtime.environment_sha256}
    )
    value = value.model_copy(
        update={"source": value.source.model_copy(update={"manifest": source})}
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        node_store_dir=str(node_store.root),
        upgrade_environment_root=str(envs),
    )
    fence_store = PrivateUpgradeStore(tmp_path / "state/upgrades")
    fence_store.write("maintenance.json", value.maintenance)
    monkeypatch.setattr(
        upgrade_smoke,
        "sys",
        SimpleNamespace(
            platform="darwin",
            executable=str(envs / "source/bin/python3"),
            prefix=str(envs / "source"),
        ),
    )
    monkeypatch.setattr(upgrade_smoke.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(upgrade_smoke.platform, "node", lambda: "coire-edge-a.local")
    engines = Mock()
    ready = EngineStatus(
        engine_id=None,
        slug=value.model_slug,
        port=9500,
        pid=123456,
        process_create_time=123.0,
        state=EngineState.FAILED if fault == "engine_readiness" else EngineState.READY,
        estimate_bytes=1024**3,
        started_at=datetime.now(UTC),
    )

    def start(**kwargs: object) -> tuple[bool, EngineStatus]:
        owned = ready.model_copy(update={"engine_id": kwargs["engine_id"]})
        engines.get.return_value = owned
        return False, owned

    engines.start.side_effect = start

    def stop(engine_id: object, **kwargs: object) -> EngineStatus:
        verify = kwargs["verify_process"]
        assert callable(verify) and verify() is False
        return ready.model_copy(update={"state": EngineState.STOPPED, "engine_id": engine_id})

    engines.stop.side_effect = stop
    engines.committed_bytes.return_value = 0
    engines.upgrade_inventory.return_value = UpgradeOwnerInventory(
        owner="engines", status="complete", entries=()
    )
    monkeypatch.setattr(upgrade_smoke, "EngineManager", lambda *_args, **_kwargs: engines)

    def process(pid: int) -> None:
        if fault == "unreadable_cleanup":
            raise psutil.AccessDenied(pid)
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(upgrade_smoke.psutil, "Process", process)
    client, response = Mock(), Mock()
    client.stream.return_value.__enter__ = Mock(return_value=response)
    client.stream.return_value.__exit__ = Mock(return_value=False)
    response.iter_bytes.return_value = [
        json.dumps(
            {
                "id": "test",
                "object": "text_completion",
                "created": 1,
                "model": "foreign-model"
                if fault == "wrong_model"
                else str(node_store.path_for(value.model_slug)),
                "choices": [
                    {
                        "text": "one two three four five six seven eight nine ten",
                        "index": 0,
                        "finish_reason": "length",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 3 if fault == "short_generation" else 10,
                    "total_tokens": 11,
                },
            }
        ).encode()
    ]
    if fault == "changed_fence":
        response.raise_for_status.side_effect = lambda: fence_store.write(
            "maintenance.json", value.maintenance.model_copy(update={"state": "released"})
        )
    elif fault == "changed_asset":
        response.raise_for_status.side_effect = lambda: (model / "weights").write_bytes(
            b"changed-model"
        )
    elif fault == "missing_asset":
        (model / "weights").unlink()
    try:
        if fault is None:
            evidence = upgrade_smoke.run_node_smoke(
                settings,
                value,
                runtime.environment_sha256,
                datetime.now(UTC) + timedelta(seconds=120),
                client,
            )
            assert evidence.cleanup_verified is True and evidence.generated_tokens == 10
            assert evidence.output_sha256 is not None
            from coire_core.models.upgrades import UpgradeEvidence

            scope = (
                Path(settings.node_state_dir)
                / "upgrades/smokes"
                / (
                    f"{value.operation.job_id}-{value.operation.generation}-{runtime.environment_sha256}-pre"
                )
            )
            assert PrivateUpgradeStore(scope).read("result.json", UpgradeEvidence) == evidence
            assert not scope.with_name(scope.name.removesuffix("-pre") + "-post").exists()
        else:
            with pytest.raises(UpgradeUnavailable):
                upgrade_smoke.run_node_smoke(
                    settings,
                    value,
                    runtime.environment_sha256,
                    datetime.now(UTC) + timedelta(seconds=120),
                    client,
                )
        if fault == "missing_asset":
            engines.start.assert_not_called()
            engines.stop.assert_not_called()
            client.stream.assert_not_called()
        else:
            engines.stop.assert_called_once()
            assert engines.stop.call_args.args == (engines.start.call_args.kwargs["engine_id"],)
            assert callable(engines.stop.call_args.kwargs["verify_process"])
            if fault == "engine_readiness":
                client.stream.assert_not_called()
            else:
                assert client.stream.call_args.kwargs["json"]["model"] == str(
                    node_store.path_for(value.model_slug)
                )
        remaining = upgrade_smoke_entries(Path(settings.node_state_dir))
        assert bool(remaining) == (fault == "unreadable_cleanup")
        scope = PrivateUpgradeStore(
            Path(settings.node_state_dir)
            / "upgrades/smokes"
            / f"{value.operation.job_id}-{value.operation.generation}-{runtime.environment_sha256}-pre"
        )
        progress = scope.read("progress.json", NativeSmokeProgress)
        assert progress is not None
        assert progress.job_id == value.operation.job_id
        assert progress.runtime_sha256 == runtime.environment_sha256
        if fault is None:
            assert progress.phase == "complete"
            assert progress.failure_phase is None and progress.failure_code is None
        elif fault == "unreadable_cleanup":
            assert progress.phase == "cleaning"
        elif fault == "engine_readiness":
            assert progress.failure_phase == "starting"
            assert progress.failure_code == "engine_readiness"
        elif fault in {"short_generation", "wrong_model", "changed_fence"}:
            assert progress.failure_phase == "generating"
            assert progress.failure_code == "verification"
        assert set(progress.model_dump()) == {
            "job_id",
            "generation",
            "runtime_sha256",
            "phase",
            "observed_at",
            "failure_phase",
            "failure_code",
        }
        assert not (Path(settings.node_state_dir) / "engines.json").exists()
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
