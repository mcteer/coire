"""Keep tokenizer imports outside the small native node control process."""

from __future__ import annotations

import asyncio
import sys
from typing import Literal

from opentelemetry import trace

_LIMIT = asyncio.Semaphore(1)
_MAX_OUTPUT = 256 * 1024
tracer = trace.get_tracer("coire.node.evaluation.tokenizer")


async def inspect_tokenizer(
    mode: Literal["identity", "probes"], payload: bytes, *, store_root: str, agent_image: str
) -> bytes:
    if len(payload) > 1024**2:
        raise ValueError("tokenizer inspection input exceeds bound")
    async with _LIMIT:
        with tracer.start_as_current_span(
            "coire.node.evaluation.tokenizer_inspect",
            attributes={"mode": mode},
            record_exception=False,
            set_status_on_exception=False,
        ):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "coire_node.evaluation_tokenizer_worker",
                mode,
                "--store-root",
                store_root,
                "--agent-image",
                agent_image,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(90):
                    assert process.stdin is not None and process.stdout is not None
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    process.stdin.close()
                    output = bytearray()
                    while chunk := await process.stdout.read(16 * 1024):
                        output.extend(chunk)
                        if len(output) > _MAX_OUTPUT:
                            raise ValueError("tokenizer inspection output exceeds bound")
                    if await process.wait() != 0:
                        raise ValueError("tokenizer inspection did not complete")
                    return bytes(output)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
