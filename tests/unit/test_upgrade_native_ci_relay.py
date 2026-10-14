"""Actual Unix/TCP bridge preserves bytes and rejects non-private endpoints."""

import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path

import pytest
from integration.upgrade_native_ci_relay import private_socket, relay


async def test_actual_bridge_preserves_large_authenticated_request_and_half_close() -> None:
    # macOS Unix socket addresses have a shorter bound than pytest's temp paths.
    temporary = "/private/tmp" if sys.platform == "darwin" else "/tmp"
    with tempfile.TemporaryDirectory(prefix="coire019-relay-", dir=temporary) as directory:
        root = await asyncio.to_thread(Path(directory).resolve)
        root.chmod(0o700)
        path = root / "control.sock"
        request = b"Authorization: Bearer synthetic-test-only\r\n" + b"x" * 150000
        received = asyncio.Event()

        async def node(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            content = await reader.read()
            assert content == request
            writer.write(hashlib.sha256(content).hexdigest().encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            received.set()

        native = await asyncio.start_unix_server(node, path=path)
        path.chmod(0o600)
        bridge = await asyncio.start_server(lambda r, w: relay(r, w, path), "127.0.0.1", 0)
        try:
            assert bridge.sockets is not None
            port = bridge.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(request)
            await writer.drain()
            writer.write_eof()
            response = await asyncio.wait_for(reader.read(), 5)
            assert response == hashlib.sha256(request).hexdigest().encode()
            await asyncio.wait_for(received.wait(), 5)
            writer.close()
            await writer.wait_closed()
            path.chmod(0o640)
            with pytest.raises(RuntimeError, match="owned private"):
                private_socket(path)
        finally:
            bridge.close()
            native.close()
            await bridge.wait_closed()
            await native.wait_closed()
