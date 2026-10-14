"""A durable backup receipt stays historical; freshness gates new migration grants."""

from datetime import UTC, datetime, timedelta

import pytest
from upgrade_fixtures import core_plan

from coire_api.upgrades.compatibility import require_migration_backup
from coire_core.errors import UpgradeConflict


def test_backup_must_match_deployed_schema_project_and_current_window() -> None:
    value = core_plan(schema_changed=True)
    assert value.backup is not None
    now = datetime.now(UTC)
    require_migration_backup(value, now=now)
    for changes in (
        {"project": "other"},
        {"schema_head": "0033_old"},
        {"created_at": now - timedelta(hours=25)},
        {"created_at": now + timedelta(minutes=1)},
        {"validated_at": now + timedelta(minutes=1)},
        {"validated_at": value.backup.created_at - timedelta(seconds=1)},
    ):
        changed = value.model_copy(update={"backup": value.backup.model_copy(update=changes)})
        with pytest.raises(UpgradeConflict):
            require_migration_backup(changed, now=now)
    with pytest.raises(UpgradeConflict):
        require_migration_backup(value.model_copy(update={"backup": None}), now=now)
    # Parsing old evidence remains possible without granting fresh migration authority.
    assert type(value).model_validate(value.model_dump(mode="json")) == value
    with pytest.raises(UpgradeConflict):
        require_migration_backup(value, now=now + timedelta(hours=25))
