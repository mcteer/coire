from pathlib import Path


def test_gateway_usage_migration_declares_reversible_revision() -> None:
    source = Path("apps/coire-api/alembic/versions/0003_gateway_usage.py").read_text()
    assert 'revision = "0003_gateway_usage"' in source
    assert 'down_revision = "0002_registry"' in source
    assert "def upgrade()" in source
    assert "def downgrade()" in source
