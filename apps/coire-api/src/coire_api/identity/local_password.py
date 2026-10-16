"""Host-operator password provisioning; never accepts passwords in argv or environment."""

import argparse
import asyncio
import getpass
import sys

from coire_api.db import dispose_engine, init_engine, session_scope
from coire_api.identity.local import provision_password
from coire_core.settings import get_settings


async def provision(email: str, password: str) -> None:
    init_engine(get_settings())
    try:
        async with session_scope() as session:
            await provision_password(session, email, password)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set/reset an existing user's local password")
    parser.add_argument("email")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read private piped input instead of prompting",
    )
    args = parser.parse_args()
    if args.password_stdin:
        password = sys.stdin.readline(4098).rstrip("\n")
    else:
        if not sys.stdin.isatty():
            parser.error("a private terminal or explicit --password-stdin is required")
        password = getpass.getpass("New password (at least 15 characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("passwords differ")
    asyncio.run(provision(args.email, password))
    print("Local password updated; previous sessions revoked.")


if __name__ == "__main__":
    main()
