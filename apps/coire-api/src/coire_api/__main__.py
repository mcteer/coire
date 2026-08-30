"""Entry point for the `coire-api` container."""

from __future__ import annotations

import uvicorn

from coire_api.app import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host="0.0.0.0",  # container-internal; only nginx is published, on loopback
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )


if __name__ == "__main__":
    main()
