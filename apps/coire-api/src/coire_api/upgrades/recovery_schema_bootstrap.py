"""Fixed one-shot migration utility invokes the actual authenticated admin API.

This uses the ordinary API image and DB concerns, with no listener, changed
network policy, rewritten migration history or SQL supplied by an operator.
"""

import asyncio
import logging
import os
import stat
import sys
from pathlib import Path

import httpx

from coire_api.app import create_app
from coire_api.db import dispose_engine, init_engine
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeRecoverySchemaBootstrapInput,
    UpgradeRecoverySchemaObservation,
    UpgradeRecoverySchemaReceipt,
)
from coire_core.settings import Settings


def read_admin_secret(settings: Settings) -> str:
    path = Path(settings.upgrade_recovery_schema_admin_token_file)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_mode & 0o022
            or details.st_size > 4096
            or details.st_uid not in {0, os.getuid()}
        ):
            raise UpgradeUnavailable(
                "Recovery schema admin credential is not a safe mounted secret"
            )
        data = os.read(descriptor, 4097)

        def identity(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_uid,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if (
            len(data) > 4096
            or identity(os.fstat(descriptor)) != identity(details)
            or identity(path.stat(follow_symlinks=False)) != identity(details)
        ):
            raise UpgradeUnavailable("Recovery schema admin credential changed during read")
        value = data.decode("utf-8").strip()
        if not value or any(char.isspace() for char in value):
            raise UpgradeUnavailable("Recovery schema admin credential is invalid")
        return value
    finally:
        os.close(descriptor)


async def invoke_admin_api(
    settings: Settings, body: UpgradeRecoverySchemaBootstrapInput
) -> UpgradeRecoverySchemaObservation | UpgradeRecoverySchemaReceipt:
    token = read_admin_secret(settings)
    init_engine(settings)
    try:
        app = create_app(settings)
        # Lifespan workers are unnecessary for this one-shot admin request. The
        # ordinary middleware, credential authentication, role/scope dependency,
        # route validation, DB transaction and audit path remain in force.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://coire-recovery-schema.internal",
            trust_env=False,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + token},
        ) as client:
            async with asyncio.timeout(15):
                response = (
                    await client.get(
                        f"/api/v1/admin/upgrades/recovery-schema/{body.original_job_id}"
                    )
                    if body.command == "observe"
                    else await client.post(
                        "/api/v1/admin/upgrades/recovery-schema",
                        json=body.preparation.model_dump(mode="json")
                        if body.preparation is not None
                        else None,
                    )
                )
        if response.status_code != 200:
            raise UpgradeUnavailable(
                f"Recovery schema admin API refused with status {response.status_code}"
            )
        return (
            UpgradeRecoverySchemaObservation.model_validate(response.json())
            if body.command == "observe"
            else UpgradeRecoverySchemaReceipt.model_validate(response.json())
        )
    finally:
        await dispose_engine()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    data = sys.stdin.buffer.read(16385)
    if len(data) > 16384:
        raise SystemExit("Recovery schema command exceeds its fixed bound")
    try:
        body = UpgradeRecoverySchemaBootstrapInput.model_validate_json(data)
        result = asyncio.run(invoke_admin_api(Settings(), body))
        print(result.model_dump_json())
    except (OSError, ValueError, UpgradeUnavailable, TimeoutError):
        raise SystemExit(
            "Recovery schema bootstrap refused; original authority remains retained"
        ) from None


if __name__ == "__main__":
    main()
