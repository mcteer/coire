"""Measured coexistence admission does not require a database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from coire_api.db import NodeRow
from coire_core.models.images import ImageCoexistenceBounds, ImageCoexistenceReportRequest
from coire_core.models.node import NodeRole, Reachability
from coire_scheduler.image_admission import (
    PINNED_RUNTIME_VERSION,
    CoexistenceReport,
    chat_mix_allowed,
    coexistence_report_hash,
    image_environment_fingerprint,
    node_hardware_fingerprint,
    node_runtime_fingerprint,
    node_thermal_alarm,
    profile_covers,
    validate_coexistence_report,
)

MEASURED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 1, 12, 5, tzinfo=UTC)
NODE_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()
CHAT_ID = uuid.uuid4()
OTHER_CHAT_ID = uuid.uuid4()
NODE_FACTS = SimpleNamespace(
    name="coire-edge-b",
    role=NodeRole.STUDIO,
    reachability=Reachability.HEALTHY,
    memory_total_bytes=128 * 1024**3,
    gpu_cores=40,
    agent_version="0.2.0",
)


def _report(**overrides: object) -> CoexistenceReport:
    values: dict[str, object] = {
        "chat_variant_ids": ("chat-a",),
        "first_token_p95_s": 1.5,
        "image_progress_observed": True,
        "measured_at": MEASURED_AT,
        "runtime_version": PINNED_RUNTIME_VERSION,
    }
    values.update(overrides)
    return CoexistenceReport(**values)  # type: ignore[arg-type]


class _Rows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return list(self._rows)


class _Session:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows
        self.statements: list[object] = []
        self.node = SimpleNamespace(**vars(NODE_FACTS))

    async def scalars(self, statement: object) -> _Rows:
        self.statements.append(statement)
        return _Rows(self._rows)

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object:
        assert model is NodeRow and kwargs == {"populate_existing": True}
        return self.node


def _profile(**overrides: object) -> SimpleNamespace:
    report = ImageCoexistenceReportRequest(
        node_id=NODE_ID,
        image_model_id=MODEL_ID,
        chat_variant_ids=(CHAT_ID, OTHER_CHAT_ID),
        hardware_fingerprint=node_hardware_fingerprint(cast(NodeRow, NODE_FACTS)),
        runtime_fingerprint=node_runtime_fingerprint(cast(NodeRow, NODE_FACTS)),
        measured_bounds=ImageCoexistenceBounds(
            max_width=512, max_height=512, max_steps=4, max_outputs=1
        ),
        duration_seconds=900,
        prompt_tokens_max=4096,
        first_token_p95_ms=1500.0,
        gateway_overhead_p95_ms=20.0,
        image_completed_count=1,
        image_progress_observed=True,
        swap_observed=False,
        thermal_alarm=False,
        runtime_version="mflux-0.20.0",
        measured_at=MEASURED_AT,
        valid_until=NOW + timedelta(hours=1),
    )
    values: dict[str, object] = {
        "status": "approved",
        "profile_hash": coexistence_report_hash(report),
        "invalidated_at": None,
        "valid_until": report.valid_until,
        "node_id": NODE_ID,
        "image_model_id": MODEL_ID,
        "chat_variant_ids": [str(CHAT_ID), str(OTHER_CHAT_ID)],
        "first_token_p95_ms": 1500.0,
        "hardware_fingerprint": report.hardware_fingerprint,
        "runtime_fingerprint": report.runtime_fingerprint,
        "image_mode": "txt2img",
        "measured_bounds": report.measured_bounds.model_dump(mode="json"),
        "benchmark_result": report.model_dump(mode="json"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_validate_rejects_failing_or_incomplete_reports() -> None:
    assert validate_coexistence_report(_report(first_token_p95_s=1.5000001)) is False
    assert validate_coexistence_report(_report(first_token_p95_s=None)) is False
    assert validate_coexistence_report(_report(first_token_p95_s=float("nan"))) is False
    assert validate_coexistence_report(_report(first_token_p95_s=float("inf"))) is False
    assert validate_coexistence_report(_report(first_token_p95_s=-0.01)) is False
    assert validate_coexistence_report(_report(image_progress_observed=False)) is False
    assert validate_coexistence_report(_report(runtime_version="mflux-0.19.0")) is False
    assert validate_coexistence_report(_report(runtime_version="mflux-0.20.0 ")) is False
    assert validate_coexistence_report(_report(runtime_version="")) is False


def test_validate_accepts_bound_with_progress_and_pinned_runtime() -> None:
    assert validate_coexistence_report(_report(first_token_p95_s=1.5)) is True
    assert validate_coexistence_report(_report(first_token_p95_s=0.0)) is True
    assert validate_coexistence_report(_report(first_token_p95_s=1.0)) is True
    assert PINNED_RUNTIME_VERSION == "mflux-0.20.0"


def test_recipe_environment_changes_with_hardware_agent_or_base_bytes() -> None:
    original = image_environment_fingerprint(cast(NodeRow, NODE_FACTS), "a" * 64)
    changed_memory = SimpleNamespace(**vars(NODE_FACTS))
    changed_memory.memory_total_bytes += 1024
    changed_agent = SimpleNamespace(**vars(NODE_FACTS))
    changed_agent.agent_version = "0.3.0"
    assert image_environment_fingerprint(cast(NodeRow, changed_memory), "a" * 64) != original
    assert image_environment_fingerprint(cast(NodeRow, changed_agent), "a" * 64) != original
    assert image_environment_fingerprint(cast(NodeRow, NODE_FACTS), "b" * 64) != original


async def test_thermal_alarm_requires_a_fresh_serious_or_critical_sample() -> None:
    class Session:
        def __init__(self) -> None:
            self.ledger = SimpleNamespace(thermal_state="serious", health_sampled_at=NOW)

        async def get(self, model: type[object], identity: object) -> object:
            assert identity == NODE_ID
            return self.ledger

    session = Session()
    assert await node_thermal_alarm(cast(Any, session), NODE_ID, NOW)
    session.ledger.health_sampled_at = NOW - timedelta(seconds=31)
    assert not await node_thermal_alarm(cast(Any, session), NODE_ID, NOW)
    session.ledger.health_sampled_at = NOW
    session.ledger.thermal_state = "nominal"
    assert not await node_thermal_alarm(cast(Any, session), NODE_ID, NOW)


@pytest.mark.parametrize(
    ("resident", "allowed", "expected"),
    [
        (set(), {"chat-a"}, True),
        ({"chat-a"}, {"chat-a", "chat-b"}, True),
        ({"chat-a", "chat-b"}, {"chat-a", "chat-b"}, True),
        ({"chat-a", "chat-c"}, {"chat-a", "chat-b"}, False),
        ({"chat-a"}, set(), False),
    ],
)
def test_profile_covers_requires_resident_subset(
    resident: set[str], allowed: set[str], expected: bool
) -> None:
    assert profile_covers(resident, allowed) is expected


async def test_empty_resident_mix_is_allowed_without_a_profile() -> None:
    session = _Session([_profile(status="draft")])
    assert (
        await chat_mix_allowed(session, uuid.uuid4(), uuid.uuid4(), set(), NOW)  # type: ignore[arg-type]
        is True
    )
    assert session.statements == []


async def test_chat_mix_allowed_requires_a_covering_current_profile() -> None:
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    covering = _profile()
    session = _Session([covering])
    assert await chat_mix_allowed(session, node_id, model_id, {str(CHAT_ID)}, NOW) is True  # type: ignore[arg-type]
    assert (
        await chat_mix_allowed(session, node_id, model_id, {str(CHAT_ID), str(OTHER_CHAT_ID)}, NOW)  # type: ignore[arg-type]
        is True
    )
    assert (
        await chat_mix_allowed(session, node_id, model_id, {str(CHAT_ID), str(uuid.uuid4())}, NOW)  # type: ignore[arg-type]
        is False
    )
    assert await chat_mix_allowed(_Session([]), node_id, model_id, {str(CHAT_ID)}, NOW) is False  # type: ignore[arg-type]

    clause = str(session.statements[0].whereclause)  # type: ignore[attr-defined]
    compiled = session.statements[0].compile()  # type: ignore[attr-defined]
    assert "status" in clause
    assert "invalidated_at IS NULL" in clause
    assert "valid_until" in clause
    assert node_id in compiled.params.values()
    assert model_id in compiled.params.values()
    assert "approved" in compiled.params.values()
    assert NOW in compiled.params.values()
    session.node.agent_version = "0.3.0"
    assert not await chat_mix_allowed(session, node_id, model_id, {str(CHAT_ID)}, NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "draft"},
        {"status": "rejected"},
        {"invalidated_at": NOW},
        {"valid_until": NOW},
        {"valid_until": NOW - timedelta(seconds=1)},
        {"chat_variant_ids": [str(OTHER_CHAT_ID)]},
        {"chat_variant_ids": str(CHAT_ID)},
        {"first_token_p95_ms": 1500.1},
        {"first_token_p95_ms": float("nan")},
        {"benchmark_result": {}},
        {"chat_variant_ids": [str(CHAT_ID), str(uuid.uuid4())]},
        {"runtime_fingerprint": "c" * 64},
        {"profile_hash": "c" * 64},
        {
            "benchmark_result": {
                "duration_seconds": 899,
                "image_progress_observed": True,
                "runtime_version": PINNED_RUNTIME_VERSION,
                "swap_observed": False,
                "thermal_alarm": False,
            }
        },
    ],
)
async def test_chat_mix_allowed_refuses_unapproved_or_uncovered_profiles(
    overrides: dict[str, object],
) -> None:
    session = _Session([_profile(**overrides)])
    assert (
        await chat_mix_allowed(session, uuid.uuid4(), uuid.uuid4(), {str(CHAT_ID)}, NOW)  # type: ignore[arg-type]
        is False
    )
