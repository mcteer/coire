"""Receipt-aware scratch cleanup and restart recovery without a native worker."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from PIL import Image
from pydantic import SecretStr

from coire_core.models.image_worker import (
    ImageTransferReceipt,
    NodeImageCleanupRequest,
    NodeImageStartRequest,
)
from coire_core.models.images import (
    ImageSpec,
    ResolvedImageSpec,
    canonical_recipe_bytes,
    canonical_spec_hash,
)
from coire_core.models.node import NetworkPath, NodePath, NodeStatus
from coire_core.settings import Settings
from coire_node.agent import create_app
from coire_node.image_dispatch import ImageNodeDispatcher
from coire_node.image_jobs import ImageJobJournal
from coire_node.image_runtime.metadata import write_image_png
from coire_node.image_runtime.supervisor import ImageProcessSupervisor
from coire_node.store import Store

JOB = "01J00000000000000000000000"
NODE = "coire-edge-b"
TOKEN = "node-secret"
MODEL = uuid.UUID("10000000-0000-0000-0000-000000000001")
INSTANCE = uuid.UUID("20000000-0000-0000-0000-000000000001")


class StubCollector:
    def latest(self, *, path: NodePath = NodePath.MESH) -> NodeStatus:
        raise AssertionError("not called")


def _fixture(
    tmp_path: Path,
) -> tuple[Settings, ImageNodeDispatcher, NodeImageCleanupRequest, Path]:
    settings = Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        node_name=NODE,
        node_token=SecretStr(TOKEN),
        node_state_dir=str(tmp_path),
        node_store_dir=str(tmp_path / "models"),
    )
    spec = ImageSpec(
        model_id=MODEL,
        prompt="fox",
        width=64,
        height=64,
        steps=1,
        guidance=Decimal("0"),
        seed=7,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )
    request = NodeImageStartRequest(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        model_id=MODEL,
        instance_id=INSTANCE,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
        reservation_bytes=1024,
    )
    journal = ImageJobJournal(tmp_path, NODE)
    queued = journal.begin(request)
    reserving = journal.advance(
        queued.model_copy(
            update={
                "state": "reserving",
                "pid": 123,
                "process_create_time": 1.0,
                "updated_at": queued.updated_at + timedelta(microseconds=1),
            }
        )
    )
    running = journal.advance(
        reserving.model_copy(
            update={
                "state": "running",
                "updated_at": reserving.updated_at + timedelta(microseconds=1),
            }
        )
    )
    journal.advance(
        running.model_copy(
            update={
                "state": "transferring",
                "updated_at": running.updated_at + timedelta(microseconds=1),
            }
        )
    )
    scratch_root = tmp_path / "image-scratch"
    scratch_root.mkdir(mode=0o700)
    attempt_dir = scratch_root / f"{JOB}-1-4"
    attempt_dir.mkdir(mode=0o700)
    path = attempt_dir / "0.png"
    image = Image.new("RGB", (64, 64), color=(1, 2, 3))
    encoded = write_image_png(image, resolved, 0, path)
    image.close()
    receipt = ImageTransferReceipt(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        index=0,
        output_id=uuid.uuid4(),
        byte_count=encoded.byte_count,
        sha256=encoded.sha256,
        recipe_sha256=hashlib.sha256(canonical_recipe_bytes(encoded.recipe)).hexdigest(),
        verified_at=datetime.now(UTC),
    )
    cleanup = NodeImageCleanupRequest(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        receipts=(receipt,),
    )
    supervisor = ImageProcessSupervisor(settings, Store(settings.node_store_dir), lambda: 0)
    return settings, ImageNodeDispatcher(journal, supervisor), cleanup, path


async def test_cleanup_requires_exact_receipt_and_replays_after_deletion(tmp_path: Path) -> None:
    settings, dispatcher, cleanup, path = _fixture(tmp_path)
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    url = f"/node/images/jobs/{JOB}/cleanup"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://node"
    ) as client:
        assert (await client.post(url, json=cleanup.model_dump(mode="json"))).status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        wrong = cleanup.model_copy(update={"fence": 5})
        assert (await client.post(url, json=wrong.model_dump(mode="json"))).status_code == 422
        bad_receipt = cleanup.receipts[0].model_copy(update={"sha256": "f" * 64})
        bad = cleanup.model_copy(update={"receipts": (bad_receipt,)})
        assert (await client.post(url, json=bad.model_dump(mode="json"))).status_code == 503
        assert path.is_file()
        first = await client.post(url, json=cleanup.model_dump(mode="json"))
        assert first.status_code == 200
        assert not path.exists()
        assert dispatcher.journal.get(JOB).state == "succeeded"  # type: ignore[union-attr]
        second = await client.post(url, json=cleanup.model_dump(mode="json"))
        assert second.status_code == 200
        assert second.json() == first.json()


async def test_cleanup_recovers_after_bytes_removed_but_before_terminal_journal(
    tmp_path: Path,
) -> None:
    settings, dispatcher, cleanup, path = _fixture(tmp_path)
    current = dispatcher.journal.get(JOB)
    assert current is not None
    dispatcher.journal.advance(
        current.model_copy(
            update={
                "receipts": cleanup.receipts,
                "updated_at": current.updated_at + timedelta(microseconds=1),
            }
        )
    )
    path.unlink()
    path.parent.rmdir()
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        result = await client.post(
            f"/node/images/jobs/{JOB}/cleanup", json=cleanup.model_dump(mode="json")
        )
    assert result.status_code == 200
    assert dispatcher.journal.get(JOB).scratch_cleaned  # type: ignore[union-attr]
