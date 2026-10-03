"""A fenced image batch is either entirely published or remains private staging."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    ApiKeyRow,
    ImageExecutionLeaseRow,
    ImageJobEventRow,
    ImageJobRow,
    ImageOutputRow,
    ImageTransferRow,
    ModelRow,
    NodeRow,
)
from coire_api.images import storage
from coire_core.errors import ImageConflict, ImageForbidden
from coire_core.models.image_worker import ImageTransferReceipt
from coire_core.models.images import (
    ImageClassificationResult,
    ImageContentTag,
    ImageJobSettingsSnapshot,
    ImageRecipe,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.models.registry import EngineBackend, ModelKind, ModelSource, ModelState, Visibility
from coire_core.settings import Settings

JOB = "01J00000000000000000000000"


def _resolved() -> ResolvedImageSpec:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private",
        width=64,
        height=64,
        steps=4,
        guidance=Decimal(0),
        seed=7,
        n=2,
    )
    return ResolvedImageSpec(
        spec=spec,
        seeds=(7, 8),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )


class Session:
    def __init__(self) -> None:
        self.owner = uuid.uuid4()
        self.node = NodeRow(id=uuid.uuid4(), name="coire-edge-b")
        self.resolved = _resolved()
        self.base = SimpleNamespace(
            kind=ModelKind.IMAGE_MODEL,
            backend=EngineBackend.MFLUX,
            source=ModelSource.STUDIO,
            state=ModelState.READY,
            visibility=Visibility.PUBLISHED,
            manifest_sha256=self.resolved.model_sha256,
        )
        self.row = ImageJobRow(
            id=JOB,
            owner_user_id=self.owner,
            state="transferring",
            attempt=1,
            fence=3,
            version=4,
            selected_node_id=self.node.id,
            instance_id=uuid.uuid4(),
            cancel_requested_at=None,
            receipt_state="complete",
            cleanup_state="cleaned",
            resolved_spec=ImageJobSettingsSnapshot(
                effective_spec=self.resolved.spec, resolved=self.resolved
            ).model_dump(mode="json"),
            authorization_snapshot={
                "required_entitlements": [],
                "explicit": False,
                "output_hold_bytes": 200,
            },
            progress=0.99,
        )
        self.lease = ImageExecutionLeaseRow(
            id=uuid.uuid4(),
            job_id=JOB,
            node_id=self.node.id,
            mode="image",
            fence=3,
            heartbeat_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        self.transfers = []
        for index in range(2):
            receipt = ImageTransferReceipt(
                job_id=JOB,
                attempt=1,
                fence=3,
                node=self.node.name,
                index=index,
                output_id=uuid.uuid4(),
                byte_count=50,
                sha256=hex(index + 1)[2:] * 64,
                recipe_sha256="c" * 64,
                verified_at=datetime.now(UTC),
            )
            self.transfers.append(
                ImageTransferRow(
                    job_id=JOB,
                    attempt=1,
                    output_index=index,
                    expected_bytes=50,
                    expected_sha256=receipt.sha256,
                    staging_key=f"image-staging/{JOB}/1/{index}.png",
                    state="received",
                    receipt=receipt.model_dump(mode="json"),
                    node_cleanup_ack_at=datetime.now(UTC),
                    lease_expires_at=datetime.now(UTC),
                    grant_hash="d" * 64,
                )
            )
        self.query_index = 0
        self.scalar_index = 0
        self.added: list[object] = []
        self.audits: list[dict[str, object]] = []

    async def execute(self, _: object) -> None:
        pass

    async def get(self, model: type[object], identity: object, **_: object) -> Any:
        if model is ImageJobRow:
            return self.row
        if model is NodeRow:
            return self.node
        if model is ModelRow:
            return self.base if identity == self.resolved.spec.model_id else None
        raise AssertionError(model)

    async def scalars(self, _: object) -> Any:
        self.query_index += 1
        result = [self.lease] if self.query_index == 1 else self.transfers
        return SimpleNamespace(all=lambda: result)

    async def scalar(self, _: object) -> Any:
        self.scalar_index += 1
        return None if self.scalar_index == 1 else 2

    def add(self, item: object) -> None:
        self.added.append(item)


def _recipe(session: Session, index: int) -> ImageRecipe:
    return ImageRecipe(
        resolved=session.resolved,
        output_index=index,
        seed=session.resolved.seeds[index],
        pixel_sha256="e" * 64,
        width=64,
        height=64,
    )


def _patch(monkeypatch: pytest.MonkeyPatch, session: Session) -> list[tuple[int, int]]:
    settled: list[tuple[int, int]] = []
    audits: list[dict[str, object]] = []

    async def principal(_: object, __: object) -> Principal:
        return Principal(kind=PrincipalKind.USER, user_id=session.owner)

    async def authorize(*_: object, **__: object) -> None:
        pass

    async def settle(_: object, __: object, held: int, stored: int) -> None:
        settled.append((held, stored))

    def verify(_: object, transfer: ImageTransferRow, __: object) -> ImageRecipe:
        return _recipe(session, transfer.output_index)

    async def audit(_: object, **kwargs: object) -> None:
        audits.append(kwargs)
        assert kwargs["action"] == "image.complete"
        assert kwargs["actor_user_id"] == session.owner
        assert kwargs["context"] == {
            "explicit": session.row.authorization_snapshot["explicit"],
            "required_entitlements": session.row.authorization_snapshot["required_entitlements"],
            "output_count": 2,
        }
        assert "private" not in str(kwargs)

    monkeypatch.setattr(storage, "_publication_principal", principal)
    monkeypatch.setattr(storage, "authorize_live_image_action", authorize)
    monkeypatch.setattr(storage, "settle_storage_hold", settle)
    monkeypatch.setattr(storage, "_verify_staged", verify)
    monkeypatch.setattr(storage, "write_audit", audit)
    session.audits = audits
    return settled


async def test_complete_batch_publishes_with_one_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    settled = _patch(monkeypatch, session)
    settings = cast(Settings, SimpleNamespace(image_blob_root="/unused"))
    assert await storage.publish_image_batch(cast(AsyncSession, session), JOB, settings)
    assert settled == [(200, 100)]
    outputs = [item for item in session.added if isinstance(item, ImageOutputRow)]
    assert len(outputs) == 2
    assert [item.blob_key for item in outputs] == [item.staging_key for item in session.transfers]
    assert all(item.state == "published" and item.content_tag == "unknown" for item in outputs)
    terminal = [item for item in session.added if isinstance(item, ImageJobEventRow)]
    assert len(terminal) == 1
    assert len(cast(list[object], terminal[0].payload["outputs"])) == 2
    assert session.row.state == "succeeded"
    assert len(session.audits) == 1
    assert session.lease.released_at is not None
    assert not await storage.publish_image_batch(cast(AsyncSession, session), JOB, settings)


async def test_published_tag_uses_studio_classification_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    classification = ImageClassificationResult(
        tag=ImageContentTag.EXPLICIT,
        score=Decimal("0.91"),
        classifier_revision="a" * 40,
        processor_sha256="b" * 64,
        tagged_at=datetime.now(UTC),
    )
    first = ImageTransferReceipt.model_validate(session.transfers[0].receipt)
    session.transfers[0].receipt = first.model_copy(
        update={"classification": classification}
    ).model_dump(mode="json")
    _patch(monkeypatch, session)
    assert await storage.publish_image_batch(
        cast(AsyncSession, session), JOB, cast(Settings, SimpleNamespace(image_blob_root="/unused"))
    )
    outputs = [item for item in session.added if isinstance(item, ImageOutputRow)]
    assert outputs[0].content_tag == "explicit"
    assert outputs[0].classifier_provenance["score"] == "0.91"
    assert outputs[1].content_tag == "unknown"


async def test_explicit_completion_audits_entitlement_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    session.row.authorization_snapshot["explicit"] = True
    session.row.authorization_snapshot["required_entitlements"] = ["explicit"]
    _patch(monkeypatch, session)
    assert await storage.publish_image_batch(
        cast(AsyncSession, session), JOB, cast(Settings, SimpleNamespace(image_blob_root="/unused"))
    )
    assert len(session.audits) == 1
    assert all(
        item.content_tag == "explicit" for item in session.added if isinstance(item, ImageOutputRow)
    )


async def test_missing_ack_or_cancel_never_publishes_a_partial_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    settled = _patch(monkeypatch, session)
    settings = cast(Settings, SimpleNamespace(image_blob_root="/unused"))
    session.transfers[1].node_cleanup_ack_at = None
    with pytest.raises(ImageConflict, match="cleanup is incomplete"):
        await storage.publish_image_batch(cast(AsyncSession, session), JOB, settings)
    assert session.added == []
    assert settled == []
    assert session.audits == []
    session.transfers[1].node_cleanup_ack_at = datetime.now(UTC)
    session.row.cancel_requested_at = datetime.now(UTC)
    with pytest.raises(ImageConflict, match="publication is not ready"):
        await storage.publish_image_batch(cast(AsyncSession, session), JOB, settings)
    assert session.added == []


async def test_retired_base_cannot_publish_durable_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    settled = _patch(monkeypatch, session)
    session.base.state = ModelState.RETIRED
    with pytest.raises(ImageConflict, match="base changed"):
        await storage.publish_image_batch(
            cast(AsyncSession, session),
            JOB,
            cast(Settings, SimpleNamespace(image_blob_root="/unused")),
        )
    assert session.added == []
    assert settled == []


async def test_corrupt_last_file_leaves_the_entire_batch_unpublished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    settled = _patch(monkeypatch, session)

    def verify(_: object, transfer: ImageTransferRow, __: object) -> ImageRecipe:
        if transfer.output_index == 1:
            raise ImageConflict("corrupt staged file")
        return _recipe(session, transfer.output_index)

    monkeypatch.setattr(storage, "_verify_staged", verify)
    with pytest.raises(ImageConflict, match="corrupt staged file"):
        await storage.publish_image_batch(
            cast(AsyncSession, session),
            JOB,
            cast(Settings, SimpleNamespace(image_blob_root="/unused")),
        )
    assert session.added == []
    assert settled == []
    assert session.row.state == "transferring"


async def test_expired_execution_lease_fences_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    settled = _patch(monkeypatch, session)
    session.lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ImageConflict, match="lease differs"):
        await storage.publish_image_batch(
            cast(AsyncSession, session),
            JOB,
            cast(Settings, SimpleNamespace(image_blob_root="/unused")),
        )
    assert session.added == []
    assert settled == []


async def test_revoked_publication_purges_staging_before_releasing_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    operations: list[str] = []

    def purge(_: object, job_id: str, attempt: int) -> None:
        assert (job_id, attempt) == (JOB, 1)
        operations.append("purge")

    async def release(_: object, __: object, held: int) -> None:
        assert held == 200
        operations.append("release")

    async def audit(*_: object, **__: object) -> None:
        operations.append("audit")

    monkeypatch.setattr(storage, "purge_terminal_transfer_staging", purge)
    monkeypatch.setattr(storage, "release_storage_hold", release)
    monkeypatch.setattr(storage, "write_audit", audit)
    settings = cast(Settings, SimpleNamespace(image_blob_root="/unused"))
    assert await storage.fail_revoked_image_batch(
        cast(AsyncSession, session), JOB, settings, safe_code="authorization_revoked"
    )
    assert operations == ["purge", "release", "audit"]
    assert session.row.state == "failed"
    assert session.row.safe_failure_code == "authorization_revoked"
    assert session.lease.released_at is not None
    assert all(item.state == "failed" for item in session.transfers)
    assert len([item for item in session.added if isinstance(item, ImageJobEventRow)]) == 1
    assert not await storage.fail_revoked_image_batch(
        cast(AsyncSession, session), JOB, settings, safe_code="authorization_revoked"
    )


async def test_revoked_publication_keeps_hold_when_node_ack_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    session.transfers[0].node_cleanup_ack_at = None
    monkeypatch.setattr(
        storage,
        "purge_terminal_transfer_staging",
        lambda *_: pytest.fail("staging must remain"),
    )
    with pytest.raises(ImageConflict, match="cleanup is incomplete"):
        await storage.fail_revoked_image_batch(
            cast(AsyncSession, session),
            JOB,
            cast(Settings, SimpleNamespace(image_blob_root="/unused")),
            safe_code="model_unavailable",
        )
    assert session.row.state == "transferring"


async def test_publication_rechecks_originating_key_version_and_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    key_id = uuid.uuid4()
    session.row.originating_key_id = key_id
    session.row.originating_key_version = 2
    key = SimpleNamespace(
        id=key_id,
        user_id=session.owner,
        credential_version=2,
        revoked_at=None,
        scopes=["images"],
    )

    async def get(model: type[object], _: object, **__: object) -> Any:
        assert model is ApiKeyRow
        return key

    monkeypatch.setattr(session, "get", get)
    principal = await storage._publication_principal(cast(AsyncSession, session), session.row)
    assert principal.scopes == frozenset({"images"})
    key.revoked_at = datetime.now(UTC)
    with pytest.raises(ImageForbidden):
        await storage._publication_principal(cast(AsyncSession, session), session.row)
    key.revoked_at = None
    key.credential_version = 3
    with pytest.raises(ImageForbidden):
        await storage._publication_principal(cast(AsyncSession, session), session.row)
