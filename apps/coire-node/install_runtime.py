"""Administrative bootstrap entrypoint; recovery uses the packaged node runtime tools."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Bootstrap runs before the node wheel exists. Its source package is a trusted build
# input; installed guards import coire_node.upgrade_runtime from their retained wheel.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from coire_node import upgrade_runtime as _runtime


def __getattr__(name: str) -> Any:
    return getattr(_runtime, name)


if __name__ == "__main__":
    _runtime.main()
