"""A repeated client intent creates at most one queued job, hold and audit."""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageJobEventRow, ImageJobRow, ModelRow
from coire_api.images import admission
from coire_api.placement.service import lock_nodes_for_admission
from coire_core.errors import ImageConflict, ImageForbidden, ImageValidationError
from coire_core.models.images import ImageCapabilityProfile, ImageContentMode, ImageSubmitRequest
from coire_core.models.registry import EngineBackend, ModelKind, ModelSource, ModelState, Visibility
from coire_core.settings import Settings

OWNER = uuid.uuid4()
MODEL = uuid.uuid4()
HIDDEN = uuid.uuid4()
JOB_ID = "01K00000000000000000000000"
PRINCIPAL = Principal(kind=PrincipalKind.USER, user_id=OWNER, subject="owner")


@pytest.mark.integration
async def test_node_admission_lock_serializes_independent_postgres_connections() -> None:
    dsn = os.environ.get("COIRE_TEST_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("set COIRE_TEST_POSTGRES_DSN for disposable local PostgreSQL")
    if not (dsn.startswith("postgresql://") or dsn.startswith("postgres://")):
        pytest.fail("node admission lock requires a PostgreSQL DSN")
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("node admission lock test requires a local PostgreSQL server")
    async_dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
        "postgres://", "postgresql+asyncpg://", 1
    )
    engine = create_async_engine(async_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    node_id = uuid.uuid4()
    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    second_locked = asyncio.Event()

    async def first() -> None:
        async with factory() as session, session.begin():
            await lock_nodes_for_admission(session, [node_id])
            first_locked.set()
            await release_first.wait()

    async def second() -> None:
        await first_locked.wait()
        async with factory() as session, session.begin():
            second_started.set()
            await lock_nodes_for_admission(session, [node_id])
            second_locked.set()

    first_task = asyncio.create_task(first())
    try:
        await asyncio.wait_for(first_locked.wait(), timeout=5)
        second_task = asyncio.create_task(second())
        try:
            await asyncio.wait_for(second_started.wait(), timeout=5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_locked.wait(), timeout=0.1)
            release_first.set()
            await asyncio.wait_for(second_locked.wait(), timeout=5)
            await second_task
        finally:
            second_task.cancel()
            await asyncio.gather(second_task, return_exceptions=True)
    finally:
        release_first.set()
        await first_task
        await engine.dispose()


def _profile(**updates: object) -> ImageCapabilityProfile:
    values: dict[str, object] = {
        "modes": ["txt2img"],
        "min_width": 256,
        "max_width": 1024,
        "min_height": 256,
        "max_height": 1024,
        "max_pixels": 1024 * 1024,
        "min_steps": 2,
        "max_steps": 30,
        "min_guidance": "0",
        "max_guidance": "4",
        "max_outputs": 2,
        "default_width": 512,
        "default_height": 512,
        "default_steps": 9,
        "default_guidance": "0",
    }
    values.update(updates)
    return ImageCapabilityProfile.model_validate(values)


class FakeSession:
    def __init__(self) -> None:
        self.existing: ImageJobRow | None = None
        self.added: list[object] = []
        self.calls: list[str] = []

    async def execute(self, statement: object) -> None:
        self.calls.append("lock")

    async def scalar(self, statement: object) -> ImageJobRow | None:
        self.calls.append("lookup")
        return self.existing

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.calls.append("flush")

    async def commit(self) -> None:
        self.calls.append("commit")
        if self.existing is None:
            self.existing = next(x for x in self.added if isinstance(x, ImageJobRow))


def test_admission_commits_once_and_replays_same_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    calls: list[tuple[str, object]] = []

    async def policy(
        session: object, request: ImageSubmitRequest, principal: Principal
    ) -> admission.ImageAdmissionPolicy:
        calls.append(("policy", request.prompt))
        return admission.ImageAdmissionPolicy(
            request=request, profile=_profile(), required_entitlements=frozenset()
        )

    async def authorize(session: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        calls.append(("auth", kwargs))
        return OWNER

    async def reserve(
        session: object, owner: uuid.UUID, outputs: int, settings: Settings, **kwargs: object
    ) -> int:
        calls.append(("reserve", outputs))
        return 2 * 64 * 1024**2

    async def audit(session: object, **kwargs: object) -> None:
        assert "private portrait" not in str(kwargs)
        calls.append(("audit", kwargs["action"]))
        assert isinstance(session, FakeSession)
        session.calls.append("audit")

    monkeypatch.setattr(admission, "_load_policy", policy)
    monkeypatch.setattr(admission, "authorize_live_image_action", authorize)
    monkeypatch.setattr(admission, "reserve_image_job_capacity", reserve)
    monkeypatch.setattr(admission, "write_audit", audit)
    monkeypatch.setattr(admission, "new_job_id", lambda: JOB_ID)
    settings = Settings(_secrets_dir="/nonexistent", image_enabled=True)  # type: ignore[call-arg]
    request = ImageSubmitRequest(model_id=MODEL, prompt="private portrait", n=2)
    first = asyncio.run(
        admission.admit_image_job(
            cast(AsyncSession, session),
            PRINCIPAL,
            request,
            "key-1",
            settings,
            random_seed=lambda: 7,
        )
    )
    second = asyncio.run(
        admission.admit_image_job(
            cast(AsyncSession, session),
            PRINCIPAL,
            request,
            "key-1",
            settings,
            random_seed=lambda: 99,
        )
    )
    assert first.job_id == second.job_id == JOB_ID
    assert [kind for kind, _ in calls].count("reserve") == 1
    assert [kind for kind, _ in calls].count("audit") == 1
    assert session.calls.index("lock") < session.calls.index("lookup")
    assert session.calls.index("audit") < session.calls.index("commit")
    job = next(x for x in session.added if isinstance(x, ImageJobRow))
    event = next(x for x in session.added if isinstance(x, ImageJobEventRow))
    effective = cast(dict[str, object], job.resolved_spec["effective_spec"])
    assert effective["seed"] == 7
    assert job.resolved_spec["resolved"] is None
    assert event.sequence == 1 and event.event_type == "queued"
    assert "private portrait" not in str(event.payload)
    with pytest.raises(ImageConflict):
        asyncio.run(
            admission.admit_image_job(
                cast(AsyncSession, session),
                PRINCIPAL,
                ImageSubmitRequest(model_id=MODEL, prompt="changed"),
                "key-1",
                settings,
            )
        )
    with pytest.raises(ImageForbidden):
        asyncio.run(
            admission.admit_image_job(
                cast(AsyncSession, session),
                PRINCIPAL,
                request,
                "key-2",
                Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            )
        )
    policy_count = [kind for kind, _ in calls].count("policy")
    with pytest.raises(ImageForbidden):
        asyncio.run(
            admission.admit_image_job(
                cast(AsyncSession, session),
                Principal(kind=PrincipalKind.SERVICE, user_id=OWNER),
                request,
                "key-3",
                settings,
            )
        )
    assert [kind for kind, _ in calls].count("policy") == policy_count


def test_direct_policy_checks_hidden_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile(required_dependency_ids=[HIDDEN])
    base = SimpleNamespace(
        id=MODEL,
        kind=ModelKind.IMAGE_MODEL,
        backend=EngineBackend.MFLUX,
        source=ModelSource.STUDIO,
        state=ModelState.READY,
        visibility=Visibility.PUBLISHED,
        image_capability_profile=profile.model_dump(mode="json"),
        entitlement=["base"],
        manifest_sha256="a" * 64,
    )
    hidden = SimpleNamespace(
        id=HIDDEN,
        kind=ModelKind.IMAGE_LORA,
        backend=EngineBackend.AUXILIARY,
        source=ModelSource.STUDIO,
        state=ModelState.READY,
        entitlement=["hidden"],
        manifest_sha256="b" * 64,
    )

    class Session:
        async def get(self, model: type[object], identity: object, **kwargs: object) -> object:
            assert model is ModelRow and kwargs.get("with_for_update") is True
            return {MODEL: base, HIDDEN: hidden}[cast(uuid.UUID, identity)]

    request = ImageSubmitRequest(model_id=MODEL, prompt="portrait")
    policy = asyncio.run(admission._load_policy(cast(AsyncSession, Session()), request, PRINCIPAL))
    assert policy.required_entitlements == frozenset({"base", "hidden"})
    base.entitlement = ["explicit"]
    explicit_policy = asyncio.run(
        admission._load_policy(cast(AsyncSession, Session()), request, PRINCIPAL)
    )
    assert explicit_policy.request.content_mode is ImageContentMode.EXPLICIT
    base.visibility = Visibility.ADMIN_ONLY
    with pytest.raises(ImageValidationError):
        asyncio.run(admission._load_policy(cast(AsyncSession, Session()), request, PRINCIPAL))
    admin = Principal(
        kind=PrincipalKind.USER, user_id=OWNER, subject="admin", scopes=frozenset({"admin"})
    )
    with pytest.raises(ImageValidationError):
        asyncio.run(admission._load_policy(cast(AsyncSession, Session()), request, admin))
    base.visibility = Visibility.PUBLISHED
    hidden.state = ModelState.FAILED
    with pytest.raises(ImageValidationError):
        asyncio.run(admission._load_policy(cast(AsyncSession, Session()), request, PRINCIPAL))
