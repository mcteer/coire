"""Public model imports preserve schemas and aliases without loading unused services."""

import json
import subprocess
import sys

import pytest
from pydantic import BaseModel

from coire_core import models
from coire_core.models.chat import ChatMessage


def test_fresh_model_namespace_does_not_load_unrequested_chat_models() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from coire_core.models import ShardingMode; "
            "print(json.dumps({'mode':ShardingMode.TENSOR_PARALLEL.value,"
            "'chat_loaded':'coire_core.models.chat' in sys.modules}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {"mode": "tp", "chat_loaded": False}


def test_all_public_exports_resolve_and_wire_schemas_remain_complete() -> None:
    for name in models.__all__:
        exported = getattr(models, name)
        assert getattr(models, name) is exported
        assert vars(models)[name] is exported
        assert name in dir(models)
        if isinstance(exported, type) and issubclass(exported, BaseModel):
            assert exported.model_json_schema()
    assert models.NativeChatMessage is ChatMessage


def test_unknown_export_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'unknown_model'"):
        _ = models.unknown_model
