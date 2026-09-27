"""Long-lived ops harness entrypoint."""

from __future__ import annotations

import uvicorn

from coire_ops.app import create_app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8003, access_log=False)


if __name__ == "__main__":
    main()
