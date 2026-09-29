"""Temporary inline images traverse the disposable scheduler and private worker."""

from __future__ import annotations

import base64
import io
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from PIL import Image

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy/compose"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1"
        or "chat-files" not in os.environ.get("COMPOSE_PROFILES", "").split(","),
        reason="run in the disposable Compose project with the private chat-files worker",
    ),
]


def test_temporary_visual_process_reuse_and_expiry() -> None:
    source = io.BytesIO()
    Image.new("RGB", (16, 16), (240, 32, 16)).save(source, format="PNG")
    encoded = base64.b64encode(source.getvalue()).decode("ascii")
    principal_id = str(uuid.uuid4())
    script = """
import asyncio
import sys
import uuid
from pathlib import Path
from sqlalchemy import select
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ChatFileProcessingRow, init_engine, session_scope
from coire_api.gateway.temporary import _expire_job, normalize_inline_images
from coire_core.models.gateway import ChatMessage, OpenAIImagePart, OpenAIImageURL
from coire_core.models.registry import VisualCapability
from coire_core.settings import get_settings

async def main():
    encoded, identity = sys.argv[1:]
    settings = get_settings()
    init_engine(settings)
    principal = Principal(kind=PrincipalKind.API_KEY, api_key_id=uuid.UUID(identity))
    visual = VisualCapability(
        verified=True, max_images=1, max_image_pixels=256, max_encoded_bytes=100000
    )
    message = ChatMessage(
        role="user",
        content=[OpenAIImagePart(image_url=OpenAIImageURL(
            url="data:image/png;base64," + encoded
        ))],
    )
    first = await normalize_inline_images([message], principal, settings, visual)
    second = await normalize_inline_images([message], principal, settings, visual)
    assert first == second
    assert first[0].content[0].image_url.url.startswith("data:image/png;base64,")
    async with session_scope() as session:
        rows = list((await session.scalars(select(ChatFileProcessingRow).where(
            ChatFileProcessingRow.principal_subject == identity
        ))).all())
    assert len(rows) == 1, len(rows)
    job = rows[0]
    original = Path(settings.chat_original_root) / job.source_key
    derived = Path(settings.chat_derived_root) / job.id
    assert job.state == "ready" and original.is_file() and derived.is_dir()
    await _expire_job(job.id)
    for _ in range(120):
        await asyncio.sleep(0.5)
        async with session_scope() as session:
            remaining = await session.get(ChatFileProcessingRow, job.id)
        if remaining is None and not original.exists() and not derived.exists():
            print("temporary image processed, reused, and erased")
            return
    raise AssertionError("expired temporary image or worker output survived cleanup")

asyncio.run(main())
"""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "coire-it",
            "exec",
            "-T",
            "coire-api",
            "/app/.venv/bin/python3",
            "-c",
            script,
            encoded,
            principal_id,
        ],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        timeout=110,
    )
    assert result.returncode == 0, result.stderr
    assert "temporary image processed, reused, and erased" in result.stdout
