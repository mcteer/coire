"""Persistent format and protected backup gates for managed upgrade authority."""

from datetime import UTC, datetime, timedelta

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeKind


def require_migration_backup(plan: UpgradeExecutionPlan, *, now: datetime | None = None) -> None:
    """Require a recent isolated-restore proof for the actual deployed source schema."""
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Backup validation time must be timezone-aware")
    backup = plan.backup
    if (
        plan.operation.kind is not UpgradeKind.CONTROL_PLANE
        or not plan.needs_migration
        or backup is None
        or backup.project != plan.source.manifest.project
        or backup.schema_head != plan.initial_schema_head
        or backup.created_at > current
        or backup.created_at < current - timedelta(hours=24)
        or backup.validated_at < backup.created_at
        or backup.validated_at > current
    ):
        raise UpgradeConflict("Migration requires a current matching restore-validated backup")
