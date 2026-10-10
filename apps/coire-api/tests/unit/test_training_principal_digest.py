"""Frozen measurement identity must survive independent process hash seeds."""

import hashlib
import json
import os
import subprocess
import sys

from pydantic import BaseModel, ConfigDict

from coire_api.auth import Principal, PrincipalKind
from coire_api.training.service import payload_digest


def test_frozen_principal_digest_is_independent_of_process_hash_seed() -> None:
    script = """
from coire_api.auth import Principal, PrincipalKind
from coire_api.training.service import payload_digest
principal = Principal(
    kind=PrincipalKind.API_KEY,
    scopes=frozenset({"admin", "chat", "training", "models"}),
    entitlements=frozenset({"coding", "general", "image"}),
    permitted_tools=frozenset({"research", "plan", "apply"}),
)
print(payload_digest(principal))
"""
    digests = {
        subprocess.check_output(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            text=True,
        ).strip()
        for seed in range(4)
    }
    assert len(digests) == 1, "Frozen principal hash differs between API and scheduler"


def test_principal_digest_retains_every_authority_field() -> None:
    principal = Principal(kind=PrincipalKind.API_KEY, scopes=frozenset({"admin", "chat"}))
    changed = principal.model_copy(update={"scopes": frozenset({"chat"})})
    assert payload_digest(principal) != payload_digest(changed)


def test_nonprincipal_wire_digests_keep_historical_serialization() -> None:
    class Document(BaseModel):
        model_config = ConfigDict(extra="forbid")
        ordered_values: tuple[str, ...]

    document = Document(ordered_values=("second", "first"))
    historical = hashlib.sha256(
        json.dumps(
            document.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert payload_digest(document) == historical
    assert payload_digest(document) != payload_digest(Document(ordered_values=("first", "second")))
