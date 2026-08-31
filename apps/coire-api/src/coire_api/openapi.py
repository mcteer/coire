"""Generate or verify the checked-in OpenAPI document."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from coire_api.app import create_app
from coire_core.settings import Settings

OUTPUT = Path(__file__).resolve().parents[2] / "openapi.json"


def rendered() -> str:
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    return json.dumps(create_app(settings).openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = rendered()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != expected:
            print(f"{OUTPUT} is stale; run: uv run python -m coire_api.openapi")
            return 1
        return 0
    OUTPUT.write_text(expected)
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
