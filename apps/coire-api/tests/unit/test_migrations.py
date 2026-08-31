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
