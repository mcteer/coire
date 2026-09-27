"""Generate the failover-only OpenAPI document used by its Studio web client."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from coire_failover.app import create_app

OUTPUT = Path(__file__).resolve().parents[2] / "openapi.json"


def rendered() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    document = rendered()
    if "--check" in sys.argv:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != document:
            print(f"{OUTPUT} is stale; run: uv run python -m coire_failover.openapi")
            raise SystemExit(1)
    else:
        OUTPUT.write_text(document, encoding="utf-8")
