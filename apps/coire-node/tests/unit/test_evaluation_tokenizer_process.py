"""Inspection owns and reaps a bounded process, including cancellation."""

import asyncio
import sys
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from coire_node.evaluation_tokenizer_process import inspect_tokenizer


@pytest.mark.parametrize("mode", ["identity", "probes"])
async def test_inspection_uses_child_and_returns_bounded_bytes(
    monkeypatch: pytest.MonkeyPatch, mode: Literal["identity", "probes"]
) -> None:
    actual = asyncio.create_subprocess_exec
    calls: list[tuple[object, ...]] = []

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        calls.append(args)
        return await actual(
            sys.executable,
            "-c",
            "import sys; sys.stdin.buffer.read(); print('{}')",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await inspect_tokenizer(mode, b"{}", store_root="/registry", agent_image="image")
    assert result.strip() == b"{}"
    assert calls[0][:3] == (sys.executable, "-m", "coire_node.evaluation_tokenizer_worker")


async def test_cancellation_kills_and_reaps_owned_child(monkeypatch: pytest.MonkeyPatch) -> None:
    actual = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    started = asyncio.Event()

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        p = await actual(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(p)
        started.set()
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(
        inspect_tokenizer("identity", b"{}", store_root="/registry", agent_image="image")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


async def test_oversized_input_is_refused_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ValueError):
        await inspect_tokenizer(
            "identity", b"x" * (1024**2 + 1), store_root="/registry", agent_image="image"
        )
    spawn.assert_not_awaited()


@pytest.mark.parametrize("program", ["print('x'*300000)", "raise SystemExit(2)"])
async def test_bad_child_output_is_refused_and_reaped(
    monkeypatch: pytest.MonkeyPatch, program: str
) -> None:
    actual = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        p = await actual(
            sys.executable,
            "-c",
            program,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ValueError):
        await inspect_tokenizer("identity", b"{}", store_root="/registry", agent_image="image")
    assert processes[0].returncode is not None
