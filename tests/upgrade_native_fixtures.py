"""Sealed byte graphs for CPU-only native pointer/recovery protocol tests."""

from datetime import UTC, datetime
from pathlib import Path

from upgrade_fixtures import plan

from coire_core.models.upgrades import (
    NativeRuntimeSelection,
    PreparedNativeRuntime,
    UpgradeExecutionPlan,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_runtime import environment_content_sha256, seal_tree


def native_plan(
    root: Path,
    *,
    paired: bool = False,
) -> tuple[UpgradeExecutionPlan, NativeRuntimeSelection, PreparedNativeRuntime]:
    envs, interpreters = root / "envs", root / "runtimes"
    interpreter = interpreters / "retained"
    (interpreter / "bin").mkdir(parents=True)
    (interpreter / "bin/coire-node-python").write_bytes(b"sealed-fixture-interpreter")
    (interpreter / "bin/coire-node-python").chmod(0o500)
    (interpreter / "bin/python3").symlink_to("coire-node-python")
    seal_tree(interpreter)
    identities = PrivateUpgradeStore(envs / ".identities")
    values = []
    for name in ("source", "target"):
        environment = envs / name
        (environment / "bin").mkdir(parents=True)
        (environment / "bin/python3").symlink_to(interpreter / "bin/python3")
        (environment / "module.py").write_bytes(name.encode())
        seal_tree(environment)
        value = PreparedNativeRuntime(
            environment_id=name,
            interpreter_id="retained",
            environment_sha256=environment_content_sha256(environment, interpreter),
            interpreter_sha256=environment_content_sha256(interpreter, interpreter),
        )
        identities.write(name + ".json", value)
        values.append(value)
    operation = plan(paired=paired)
    source_manifest = operation.source.manifest.model_copy(
        update={"environment_sha256": values[0].environment_sha256}
    )
    source = operation.source.model_copy(
        update={"manifest": source_manifest, "manifest_sha256": source_manifest.sha256()}
    )
    target_manifest = operation.target.manifest.model_copy(
        update={
            "environment_sha256": values[1].environment_sha256,
            "compatibility": operation.target.manifest.compatibility.model_copy(
                update={"source_sha256s": (source.manifest_sha256,)}
            ),
        }
    )
    target = operation.target.model_copy(
        update={"manifest": target_manifest, "manifest_sha256": target_manifest.sha256()}
    )
    operation = UpgradeExecutionPlan.model_validate(
        operation.model_dump(mode="json")
        | {
            "source": source,
            "target": target,
            "operation": operation.operation.model_copy(
                update={
                    "source_sha256": source.manifest_sha256,
                    "target_sha256": target.manifest_sha256,
                }
            ),
        }
    )
    selected = NativeRuntimeSelection(
        node="coire-edge-a",
        release_sha256=source.manifest_sha256,
        runtime=values[0],
        generation=0,
        selected_at=datetime.now(UTC),
    )
    (envs / "current").symlink_to(envs / "source")
    return operation, selected, values[1]
