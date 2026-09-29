"""Chat coding inputs bind owner previews and erase their durable byte copy."""

from __future__ import annotations

import base64
import hashlib
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.chat.coding import _coding_visual_inputs
from coire_api.coding_calls import CodingRequest, create_coding_call
from coire_api.db import ChatAttachmentRow, McpCallRow, ModelRow
from coire_api.run_executor import _coding_visual_inputs as prepared_run_visual_inputs
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import ChatTurnCreate
from coire_core.models.files import FileProcessAsset, FileProcessResult
from coire_core.models.mcp import (
    McpCallState,
    McpToolName,
    ResearchInput,
    ResearchResult,
    WorkspaceSource,
)
from coire_core.models.registry import EngineBackend, VisualCapability
from coire_core.settings import Settings


async def test_coding_visual_input_is_owned_and_copied_from_verified_worker_asset(
    tmp_path: Path,
) -> None:
    owner, conversation, file_id, asset_id = (uuid.uuid4() for _ in range(4))
    job_id = "01K00000000000000000000000"
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(output, format="PNG")
    data = output.getvalue()
    folder = tmp_path / job_id
    folder.mkdir()
    (folder / f"{asset_id}.png").write_bytes(data)
    result = FileProcessResult(
        job_id=job_id,
        input_id=file_id,
        source_sha256="a" * 64,
        detected_type="image/png",
        assets=[
            FileProcessAsset(
                id=asset_id,
                sha256=hashlib.sha256(data).hexdigest(),
                bytes=len(data),
                media_type="image/png",
                width=16,
                height=16,
            )
        ],
    )
    attachment = ChatAttachmentRow(
        id=file_id,
        owner_user_id=owner,
        conversation_id=conversation,
        filename="red.png",
        detected_type="image/png",
        original_bytes=len(data),
        original_sha256="a" * 64,
        original_key=str(file_id),
        derived_bytes=len(data),
        state="ready",
        asset_manifest={"job_id": job_id, "result": result.model_dump(mode="json")},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class Session:
        async def get(self, model: object, identifier: uuid.UUID) -> ChatAttachmentRow | None:
            return attachment if model is ChatAttachmentRow and identifier == file_id else None

    model = ModelRow(
        id=uuid.uuid4(),
        backend=EngineBackend.MLX_VLM,
        visual_capability=VisualCapability(
            verified=True, max_images=1, max_image_pixels=256, max_encoded_bytes=len(data)
        ).model_dump(mode="json"),
    )
    body = ChatTurnCreate(
        client_request_id=uuid.uuid4(),
        expected_revision=1,
        model_id=model.id,
        content="Inspect this image",
        workspace_id=uuid.uuid4(),
        action="research",
        attachments=[{"file_id": file_id, "mode": "visual"}],  # type: ignore[list-item]
    )
    settings = Settings(chat_derived_root=str(tmp_path), _secrets_dir="/nonexistent")  # type: ignore[call-arg]
    staged = await _coding_visual_inputs(
        cast(AsyncSession, Session()),
        Principal(kind=PrincipalKind.USER, user_id=owner),
        conversation,
        body,
        model,
        settings,
    )
    assert len(staged) == 1 and staged[0].asset_id == asset_id
    assert base64.b64decode(staged[0].data_base64) == data
    assert staged[0].sha256 == hashlib.sha256(data).hexdigest()

    class CallSession:
        def __init__(self) -> None:
            self.row: McpCallRow | None = None

        def add(self, row: McpCallRow) -> None:
            self.row = row

        async def flush(self) -> None:
            assert self.row is not None
            self.row.id = uuid.uuid4()

    call_session = CallSession()
    recorded = await create_coding_call(
        cast(AsyncSession, call_session),
        owner_user_id=owner,
        credential_id=None,
        request=CodingRequest(
            McpToolName.RESEARCH,
            ResearchInput(
                source=WorkspaceSource(workspace_id=uuid.uuid4(), revision="HEAD"),
                question="inspect",
                model_id=model.id,
            ),
            "inspect",
            staged,
        ),
        model_id=model.id,
    )
    assert recorded.input["coire_visual_inputs"][0]["data_base64"] == staged[0].data_base64
    call = McpCallRow(input={"coire_visual_inputs": [staged[0].model_dump(mode="json")]})
    assert prepared_run_visual_inputs(call) == list(staged)
    call.input = {
        "coire_visual_inputs": [{**call.input["coire_visual_inputs"][0], "sha256": "0" * 64}]
    }
    with pytest.raises(ValueError, match="manifest"):
        prepared_run_visual_inputs(call)
    with pytest.raises(ChatNotFound):
        await _coding_visual_inputs(
            cast(AsyncSession, Session()),
            Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
            conversation,
            body,
            model,
            settings,
        )
    model.visual_capability = VisualCapability(
        verified=True, max_images=1, max_image_pixels=256, max_encoded_bytes=len(data) - 1
    ).model_dump(mode="json")
    with pytest.raises(ChatConflict, match="measured visual limits"):
        await _coding_visual_inputs(
            cast(AsyncSession, Session()),
            Principal(kind=PrincipalKind.USER, user_id=owner),
            conversation,
            body,
            model,
            settings,
        )


async def test_coding_result_scrubs_private_visual_bytes() -> None:
    from coire_api.mcp_calls import fail_call, store_result

    class Session:
        async def flush(self) -> None:
            pass

    run_id = uuid.uuid4()
    call = McpCallRow(
        id=uuid.uuid4(),
        tool=McpToolName.RESEARCH,
        run_id=run_id,
        state=McpCallState.RUNNING,
        input={"question": "inspect", "coire_visual_inputs": [{"data_base64": "private"}]},
    )
    result = ResearchResult(
        result_id=uuid.uuid4(),
        run_id=run_id,
        source_revision="a" * 40,
        answer="ok",
        citations=[{"path": "main.py", "line": 1}],  # type: ignore[list-item]
    )
    await store_result(cast(AsyncSession, Session()), row=call, result=result)
    assert call.input == {"question": "inspect"}
    failed = McpCallRow(
        id=uuid.uuid4(),
        tool=McpToolName.RESEARCH,
        run_id=uuid.uuid4(),
        state=McpCallState.RUNNING,
        input={"question": "inspect", "coire_visual_inputs": [{"data_base64": "private"}]},
    )
    await fail_call(
        cast(AsyncSession, Session()), row=failed, state=McpCallState.FAILED, code="test"
    )
    assert failed.input == {"question": "inspect"}
