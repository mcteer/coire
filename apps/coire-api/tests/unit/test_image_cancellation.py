"""Owner cancellation commits intent and retains uncertain reservations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageJobEventRow, ImageJobRow
from coire_api.images import cancellation
from coire_core.errors import ImageForbidden, ImageNotFound
from coire_core.models.images import (
    ImageJob,
    ImageJobSettingsSnapshot,
    ImageJobState,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)

OWNER = uuid.uuid4()
OTHER = uuid.uuid4()
MODEL = uuid.uuid4()
JOB = "01J00000000000000000000000"
PRINCIPAL = Principal(kind=PrincipalKind.USER, user_id=OWNER)


def _job(*, state: str = "queued", fence: int = 0, owner_id: uuid.UUID = OWNER) -> Any:
    spec = ImageSpec(
        model_id=MODEL,
        prompt="private subject",
        width=512,
        height=512,
        steps=9,
        guidance=Decimal(0),
        seed=7,
    )
    resolved = (
        ResolvedImageSpec(
            spec=spec,
            seeds=(7,),
            pipeline_version="mflux-0.20.0",
            environment_fingerprint="a" * 64,
            model_sha256="b" * 64,
            spec_hash=canonical_spec_hash(spec),
        )
        if state == "running"
        else None
    )
    snapshot = ImageJobSettingsSnapshot(effective_spec=spec, resolved=resolved)
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=JOB,
        owner_user_id=owner_id,
        state=state,
        fence=fence,
        selected_node_id=None,
        instance_id=None,
        reservation_ids=[],
        resolved_spec=snapshot.model_dump(mode="json"),
        authorization_snapshot={
            "required_entitlements": [],
            "explicit": False,
            "output_hold_bytes": 64,
        },
        safe_failure_code=None,
        version=1,
        created_at=now,
        updated_at=now,
        finished_at=None,
        cancel_requested_at=None,
        receipt_state="pending",
        cleanup_state="pending",
        progress=0.0,
    )


class Session:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.added: list[object] = []
        self.calls: list[str] = []

    async def execute(self, statement: object) -> None:
        self.calls.append("quota_lock")

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert model is ImageJobRow and identity == JOB
        assert kwargs == {"populate_existing": True, "with_for_update": True}
        self.calls.append("job_lock")
        return self.row

    async def scalar(self, statement: object) -> int:
        return 1

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.calls.append("commit")


def _patch(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    async def authorize(session: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        calls.append("authorize")
        return OWNER

    async def release(session: object, owner: uuid.UUID, outputs: int, held: int) -> None:
        calls.append("release")
        assert (owner, outputs, held) == (OWNER, 1, 64)

    async def audit(session: object, **kwargs: object) -> None:
        calls.append("audit")
        assert kwargs["action"] == "image.cancel"

    monkeypatch.setattr(cancellation, "authorize_live_image_action", authorize)
    monkeypatch.setattr(cancellation, "release_pending_image_job_capacity", release)
    monkeypatch.setattr(cancellation, "write_audit", audit)


async def test_unplaced_queued_cancel_releases_hold_and_commits_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _job()
    session = Session(row)
    calls: list[str] = []
    _patch(monkeypatch, calls)
    result, terminal = await cancellation.request_image_job_cancel(
        cast(AsyncSession, session), PRINCIPAL, JOB
    )
    assert terminal and result.state is ImageJobState.CANCELLED
    assert result.latest_event_sequence == 2
    assert row.cleanup_state == "cleaned" and row.cancel_requested_at is not None
    assert calls == ["authorize", "release", "audit"]
    assert session.calls.index("quota_lock") < session.calls.index("job_lock")
    assert len(session.added) == 1
    event = cast(ImageJobEventRow, session.added[0])
    assert event.event_type == "cancelled" and event.sequence == 2
    assert "private subject" not in str(event.payload)
    assert session.calls[-1] == "commit"


@pytest.mark.parametrize("state,fence", [("running", 3), ("queued", 3)])
async def test_uncertain_cancel_retains_hold_until_node_ack(
    monkeypatch: pytest.MonkeyPatch, state: str, fence: int
) -> None:
    row = _job(state=state, fence=fence)
    session = Session(row)
    calls: list[str] = []
    _patch(monkeypatch, calls)
    result, terminal = await cancellation.request_image_job_cancel(
        cast(AsyncSession, session), PRINCIPAL, JOB
    )
    assert not terminal and result.state is ImageJobState.CANCELLING
    assert row.cancel_requested_at is not None
    assert row.cleanup_state == "pending"
    assert calls == ["authorize", "audit"]
    assert session.added == []
    assert session.calls[-1] == "commit"


async def test_wrong_owner_or_revocation_never_mutates_job(monkeypatch: pytest.MonkeyPatch) -> None:
    wrong = Session(_job(owner_id=OTHER))
    with pytest.raises(ImageNotFound):
        await cancellation.request_image_job_cancel(cast(AsyncSession, wrong), PRINCIPAL, JOB)
    assert wrong.calls == ["quota_lock", "job_lock"]

    async def revoked(session: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        raise ImageForbidden()

    monkeypatch.setattr(cancellation, "authorize_live_image_action", revoked)
    denied = Session(_job())
    with pytest.raises(ImageForbidden):
        await cancellation.request_image_job_cancel(cast(AsyncSession, denied), PRINCIPAL, JOB)
    assert denied.row.state == "queued" and denied.added == []


@pytest.mark.parametrize("state, terminal", [("cancelling", False), ("cancelled", True)])
async def test_repeated_cancel_does_not_charge_or_append_events(
    monkeypatch: pytest.MonkeyPatch, state: str, terminal: bool
) -> None:
    row = _job(state=state)
    session = Session(row)
    calls: list[str] = []
    _patch(monkeypatch, calls)

    async def current(db: object, principal: Principal, job_id: str) -> ImageJob:
        calls.append("read")
        snapshot = ImageJobSettingsSnapshot.model_validate(row.resolved_spec)
        return ImageJob(
            id=JOB,
            state=ImageJobState(state),
            effective_spec=snapshot.effective_spec,
            resolved=snapshot.resolved,
            latest_event_sequence=1,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    monkeypatch.setattr(cancellation, "get_owned_image_job", current)
    result, was_terminal = await cancellation.request_image_job_cancel(
        cast(AsyncSession, session), PRINCIPAL, JOB
    )
    assert result.state == state and was_terminal is terminal
    assert calls == ["authorize", "read"]
    assert session.added == [] and "commit" not in session.calls


async def test_human_admin_can_kill_other_owners_revoked_placed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _job(state="running", fence=3, owner_id=OTHER)
    session = Session(row)
    actions: list[str] = []

    async def forbidden_owner_check(*args: object, **kwargs: object) -> uuid.UUID:
        raise AssertionError("admin kill must not depend on the revoked owner's entitlement")

    async def audit(_session: object, **kwargs: object) -> None:
        actions.append(str(kwargs["action"]))
        context = kwargs["context"]
        assert isinstance(context, dict)
        assert context["owner_id"] == str(OTHER)

    monkeypatch.setattr(cancellation, "authorize_live_image_action", forbidden_owner_check)
    monkeypatch.setattr(cancellation, "write_audit", audit)
    admin = Principal(kind=PrincipalKind.ADMIN, user_id=OWNER)
    result, terminal = await cancellation.request_admin_image_job_cancel(
        cast(AsyncSession, session), admin, JOB
    )
    assert not terminal and result.state is ImageJobState.CANCELLING
    assert row.cancel_requested_at is not None and row.cleanup_state == "pending"
    assert actions == ["image.admin.cancel"]
    assert session.calls[-1] == "commit"


async def test_service_identity_cannot_call_admin_image_kill() -> None:
    session = Session(_job())
    with pytest.raises(ImageForbidden):
        await cancellation.request_admin_image_job_cancel(
            cast(AsyncSession, session),
            Principal(kind=PrincipalKind.SERVICE, user_id=OWNER, scopes=frozenset({"admin"})),
            JOB,
        )
    assert session.calls == []
