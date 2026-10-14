"""CI-only byte relay from a private Docker network to owned SSH Unix sockets.

Authentication remains at the native node. No engine address is selected or
published by this relay, and no HTTP headers or credentials are inspected.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import stat
from pathlib import Path


def private_socket(path: Path) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or path.parent.stat().st_mode & 0o077
        or path.parent.stat().st_uid != os.getuid()
    ):
        raise RuntimeError("Native CI relay requires an owned private Unix socket")


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: Path) -> None:
    peer_writer: asyncio.StreamWriter | None = None
    tasks: list[asyncio.Task[None]] = []
    try:
        private_socket(path)
        peer_reader, peer_writer = await asyncio.open_unix_connection(path, limit=65536)

        async def copy(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
            while chunk := await source.read(65536):
                target.write(chunk)
                await target.drain()
            if target.can_write_eof():
                target.write_eof()

        tasks = [
            asyncio.create_task(copy(reader, peer_writer)),
            asyncio.create_task(copy(peer_reader, writer)),
        ]
        await asyncio.gather(*tasks)
    except (OSError, RuntimeError):
        # The ordinary authenticated node client observes a closed transport;
        # ownership and readiness stay fail-closed when a bridge disappears.
        pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        writers = [writer] + ([peer_writer] if peer_writer is not None else [])
        for endpoint in writers:
            endpoint.close()
        await asyncio.gather(
            *(endpoint.wait_closed() for endpoint in writers), return_exceptions=True
        )


async def serve(control: Path, data: Path) -> None:
    private_socket(control)
    private_socket(data)
    servers = [
        await asyncio.start_server(lambda r, w: relay(r, w, control), "0.0.0.0", 9400),
        await asyncio.start_server(lambda r, w: relay(r, w, data), "0.0.0.0", 9401),
    ]
    try:
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("control", type=Path)
    parser.add_argument("data", type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.control, args.data))
