"""One-process service entrypoint and credentialed local health probe."""

from __future__ import annotations

import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

import uvicorn

from coire_core.settings import Settings


def main() -> int:
    if sys.argv[1:] == ["--healthcheck"]:
        token = Settings().file_worker_service_token.get_secret_value()
        if not token:
            return 1
        request = Request(
            "http://127.0.0.1:8010/health", headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urlopen(request, timeout=2) as response:
                return 0 if response.status == 200 else 1
        except (OSError, URLError):
            return 1
    if len(sys.argv) != 1:
        return 2
    uvicorn.run("coire_file_worker.app:app", host="0.0.0.0", port=8010, workers=1, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
