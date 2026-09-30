"""Repository image templates are inert until an admin binds a registry UUID."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from coire_core.models.images import ImagePresetCreate

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "recipes" / "images"


def test_image_templates_require_binding_and_use_only_preset_fields() -> None:
    files = sorted(TEMPLATES.glob("*.json"))
    assert len(files) >= 2
    for path in files:
        payload = json.loads(path.read_text())
        assert set(payload) <= {"name", "prompt_prefix", "defaults"}
        assert payload["defaults"]["model_id"] == "__MODEL_ID__"
        assert set(payload["defaults"]) <= {
            "model_id",
            "mode",
            "prompt",
            "negative_prompt",
            "width",
            "height",
            "steps",
            "guidance",
            "n",
            "output",
            "content_mode",
        }
        with pytest.raises(ValidationError):
            ImagePresetCreate.model_validate(payload)
        bound = {**payload, "defaults": {**payload["defaults"], "model_id": str(uuid.uuid4())}}
        validated = ImagePresetCreate.model_validate(bound)
        assert validated.defaults.model_id is not None
        assert validated.defaults.preset_id is None
        assert validated.defaults.content_mode is None
