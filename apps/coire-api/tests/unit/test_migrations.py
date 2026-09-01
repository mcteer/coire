"""Migration shape tests that do not require a live Postgres instance."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

MIGRATION = Path(__file__).parents[2] / "alembic" / "versions" / "0003_node_endpoints.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("node_endpoints_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recorder:
    def __init__(self) -> None:
        self.added: list[tuple[str, Any]] = []
        self.dropped: list[tuple[str, str]] = []
        self.altered: list[tuple[str, str, dict[str, Any]]] = []

    def add_column(self, table: str, column: Any) -> None:
        self.added.append((table, column))

    def drop_column(self, table: str, column: str) -> None:
        self.dropped.append((table, column))

    def alter_column(self, table: str, column: str, **kwargs: Any) -> None:
        self.altered.append((table, column, kwargs))


def test_node_endpoint_migration_is_additive_and_nullable() -> None:
    migration = _migration()
    recorder = Recorder()
    migration.op = recorder  # type: ignore[attr-defined]
    migration.upgrade()

    assert [column.name for _, column in recorder.added] == [
        "endpoint_contract_version",
        "control_host",
        "data_host",
    ]
    assert all(table == "nodes" and column.nullable for table, column in recorder.added)
    assert recorder.altered == [("nodes", "mesh_address", {"nullable": True})]


def test_node_endpoint_migration_downgrade_reverses_columns() -> None:
    migration = _migration()
    recorder = Recorder()
    migration.op = recorder  # type: ignore[attr-defined]
    migration.downgrade()

    assert recorder.dropped == [
        ("nodes", "data_host"),
        ("nodes", "control_host"),
        ("nodes", "endpoint_contract_version"),
    ]
    assert recorder.altered == [("nodes", "mesh_address", {"nullable": False})]


def test_gateway_usage_migration_declares_reversible_revision() -> None:
    source = Path("apps/coire-api/alembic/versions/0003_gateway_usage.py").read_text()
    assert 'revision = "0003_gateway_usage"' in source
    assert 'down_revision = "0002_registry"' in source
    assert "def upgrade()" in source
    assert "def downgrade()" in source


def test_gateway_and_fabric_migration_heads_are_merged() -> None:
    source = Path("apps/coire-api/alembic/versions/0004_merge_gateway_fabrics.py").read_text()
    assert 'revision = "0004_merge_gateway_fabrics"' in source
    assert 'down_revision = ("0003_gateway_usage", "0003_node_endpoints")' in source


def test_acquisition_variant_migration_is_additive_and_guards_downgrade() -> None:
    source = Path("apps/coire-api/alembic/versions/0005_acquisition_variants.py").read_text()
    assert 'revision = "0005_acquisition_variants"' in source
    assert 'down_revision = "0004_merge_gateway_fabrics"' in source
    for table in (
        "model_variants",
        "acquisition_workflows",
        "acquisition_stages",
        "inspection_results",
        "validation_results",
        "variant_copies",
        "node_reservations",
    ):
        assert f'"{table}"' in source
    assert "cannot downgrade after creating additional model variants" in source


def test_memory_ledger_migration_is_reversible() -> None:
    source = Path("apps/coire-api/alembic/versions/0006_memory_ledger.py").read_text()
    assert 'revision: str = "0006_memory_ledger"' in source
    assert 'down_revision: str | None = "0005_acquisition_variants"' in source
    for table in (
        "node_memory_ledgers",
        "memory_reservations",
        "request_leases",
        "placement_decisions",
        "eviction_events",
        "placement_commands",
    ):
        assert f'"{table}"' in source
    assert '"placement_commands",\n        "eviction_events"' in source


def test_model_instance_migration_preserves_legacy_engines_and_is_reversible() -> None:
    source = Path("apps/coire-api/alembic/versions/0007_model_instances.py").read_text()
    assert 'revision: str = "0007_model_instances"' in source
    assert 'down_revision: str | None = "0006_memory_ledger"' in source
    for table in (
        "model_instances",
        "instance_members",
        "instance_transitions",
        "registration_attempts",
    ):
        assert f'"{table}"' in source
    assert "UPDATE engine_processes SET instance_id=id" in source
    assert "UPDATE memory_reservations r SET holder_id=e.instance_id::text" in source
    assert 'op.drop_column("engine_processes", "instance_id")' in source


def test_sharded_serving_migration_is_reversible() -> None:
    source = Path("apps/coire-api/alembic/versions/0008_sharded_serving.py").read_text()
    assert 'revision: str = "0008_sharded_serving"' in source
    assert 'down_revision: str | None = "0007_model_instances"' in source
    for table in (
        "link_observations",
        "shard_groups",
        "shard_commands",
        "benchmark_runs",
        "placement_benchmarks",
        "benchmark_commands",
    ):
        assert f'"{table}"' in source
        assert f'op.drop_table("{table}")' in source
