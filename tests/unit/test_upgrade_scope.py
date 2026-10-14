"""Only release dependencies may drain Studios; unrelated chat remains admitted."""

import pytest

from coire_api.upgrades.admission import affected_services
from coire_core.models.upgrades import ServiceName, UpgradeKind
from coire_scheduler.maintenance import dependency_scope


def test_unrelated_cohort_preserves_studio_admission() -> None:
    scope = dependency_scope(UpgradeKind.CONTROL_PLANE, node=None, cohort=("coire-file-worker",))
    assert scope == ("coire-file-worker",)
    assert not set(scope).intersection(affected_services("POST", "/v1/chat/completions"))
    assert set(scope).intersection(affected_services("POST", "/api/v1/chat/files"))


def test_schema_and_renewal_owner_require_both_studios() -> None:
    cases: tuple[tuple[tuple[ServiceName, ...], bool], ...] = (
        (("coire-api",), False),
        (("coire-scheduler",), False),
        (("coire-web",), False),
        (("coire-web",), True),
    )
    for cohort, schema in cases:
        scope = dependency_scope(
            UpgradeKind.CONTROL_PLANE, node=None, cohort=cohort, schema_changed=schema
        )
        assert {"core", "coire-edge-a", "coire-edge-b"}.issubset(scope)


def test_single_studio_and_pair_are_explicit() -> None:
    assert dependency_scope(UpgradeKind.STUDIO, node="coire-edge-a") == ("coire-edge-a",)
    assert dependency_scope(UpgradeKind.STUDIO, node="coire-edge-a", paired=True) == (
        "coire-edge-a",
        "coire-edge-b",
    )


def test_cleanup_and_recovery_are_not_new_work() -> None:
    for path in (
        "/api/v1/runs/example/cancel",
        "/api/v1/internal/training/lease",
        "/api/v1/admin/upgrades/example/cancel",
    ):
        assert affected_services("POST", path) == ()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/keys/10000000-0000-4000-8000-000000000001",
        "/api/v1/admin/runs/10000000-0000-4000-8000-000000000001",
        "/api/v1/images/01ARZ3NDEKTSV4RRFFQ69G5FAV",
    ],
)
def test_existing_canonical_delete_controls_remain_callable(path: str) -> None:
    assert affected_services("DELETE", path) == ()
    for method in ("GET", "POST", "PUT", "PATCH"):
        assert "coire-api" in affected_services(method, path)
    assert "coire-api" in affected_services("DELETE", path + "/extra")


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/keys/not-a-key",
        "/api/v1/admin/runs/not-a-run",
        "/api/v1/admin/keys",
        "/api/v1/images/not-a-job",
        "/api/v1/images/inputs/10000000-0000-4000-8000-000000000001",
    ],
)
def test_delete_control_exemption_does_not_widen_to_other_mutations(path: str) -> None:
    assert "coire-api" in affected_services("DELETE", path)


def test_get_streams_hold_api_drain_without_blocking_upgrade_observation() -> None:
    for path in (
        "/api/v1/runs/example/events",
        "/api/v1/admin/training/jobs/example/events",
        "/api/v1/images/jobs/example/events",
    ):
        assert "coire-api" in affected_services("GET", path)
        assert "coire-file-worker" not in affected_services("GET", path)
    for path in ("/health", "/ready", "/api/v1/admin/upgrades/jobs/example/events"):
        assert affected_services("GET", path) == ()


async def test_fence_uses_actual_retained_schema_instead_of_image_migration_head() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch

    from sqlalchemy.ext.asyncio import AsyncSession
    from upgrade_fixtures import core_plan

    from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeMaintenance
    from coire_scheduler.maintenance import fence

    value = core_plan(schema_changed=True)
    operation = value.operation.model_copy(update={"cohort": ("coire-file-worker",)})
    value = UpgradeExecutionPlan.model_validate(
        value.model_dump(mode="json")
        | {
            "operation": operation,
            "schema_head_after": value.initial_schema_head,
            "maintenance": value.maintenance.model_dump(mode="json")
            | {"targets": ["coire-file-worker"]},
        }
    )
    assert not value.needs_migration
    session = AsyncMock(spec=AsyncSession)
    rows = Mock()
    rows.__iter__ = Mock(return_value=iter([]))
    session.scalars.return_value = rows
    session.get.return_value = None
    with (
        patch(
            "coire_scheduler.maintenance.get_operation",
            AsyncMock(
                return_value=SimpleNamespace(
                    payload=operation.model_dump(mode="json"),
                    execution_plan=value.model_dump(mode="json"),
                )
            ),
        ),
        patch(
            "coire_scheduler.maintenance.get_release",
            AsyncMock(side_effect=[value.source, value.target]),
        ),
        patch("coire_api.placement.service.lock_nodes_for_admission", AsyncMock()),
    ):
        await fence(session, UpgradeMaintenance.model_validate(value.maintenance))
    assert session.add.call_count == 1
    assert session.add.call_args.args[0].target == "coire-file-worker"
