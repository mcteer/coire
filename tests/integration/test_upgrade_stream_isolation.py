"""100 real gateway connections survive unrelated composed service restarts.

The existing Linux engine double controls stream timing; this proves transport
isolation, not model qualification. Real tiny-model qualification is a separate
required native gate. This fixture never contacts operator Studios.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from conftest import COMPOSE_DIR, PROJECT, integration_env
from integration.test_gateway import gateway_model as gateway_model

pytestmark = [pytest.mark.integration, pytest.mark.upgrade_stream]


async def test_one_hundred_streams_survive_unrelated_service_restarts(
    api_url: str, gateway_model: str, admin_headers: dict[str, str], tmp_path: Path
) -> None:
    if os.environ.get("COIRE_INTEGRATION") != "1":
        raise RuntimeError("Required stream gate needs its isolated composed fixture")
    if os.environ.get("COIRE_IT_GATEWAY_STREAM_CAPACITY") != "100":
        raise RuntimeError("Required stream gate needs its 100-slot controlled engine fixture")
    barrier = asyncio.Event()
    ready = asyncio.Event()
    count = 0
    finished = 0
    outcomes = []
    async with httpx.AsyncClient(
        base_url=api_url,
        headers=admin_headers,
        timeout=120,
        limits=httpx.Limits(max_connections=110, max_keepalive_connections=110),
        trust_env=False,
    ) as client:
        # Require actual serving before testing unrelated restarts; concurrent
        # cold loading is a separate admission case.
        warmed = await client.post(
            "/v1/chat/completions",
            json={
                "model": gateway_model,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
        )
        assert warmed.status_code == 200, warmed.text[:512]

        async def stream(index: int) -> None:
            nonlocal count, finished
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": gateway_model,
                    "stream": True,
                    "messages": [{"role": "user", "content": "upgrade-stream"}],
                    "max_tokens": 16,
                },
            ) as response:
                assert response.status_code == 200, (await response.aread()).decode()[:512]
                lines = response.aiter_lines()
                first = await asyncio.wait_for(anext(lines), timeout=60)
                assert first.startswith("data:")
                count += 1
                if count == 100:
                    ready.set()
                await barrier.wait()
                body = first + "\n" + "\n".join([line async for line in lines])
                assert "[DONE]" in body and '"error"' not in body
                assert '"content"' in body
                outcomes.append({"stream": index, "completed": True})
                finished += 1

        tasks = [asyncio.create_task(stream(i)) for i in range(100)]
        measurements = []
        ready_task = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait(
                [ready_task, *tasks], timeout=90, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not ready_task:
                    task.result()  # Surface admission/transport failure immediately.
                    raise AssertionError("A stream ended before the concurrent restart barrier")
            assert ready_task in done, f"Only {count} concurrent streams became ready"
            assert count == 100 and finished == 0
            for service in ("coire-mcp", "coire-scheduler", "coire-ops"):
                start = time.monotonic()
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "compose", "-p", PROJECT, "restart", service],
                    cwd=COMPOSE_DIR,
                    env=integration_env(),
                    capture_output=True,
                    timeout=30,
                )
                assert result.returncode == 0, "Owned unrelated service restart failed"
                measurements.append({"service": service, "elapsed_s": time.monotonic() - start})
            barrier.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=120)
            assert finished == 100
        finally:
            barrier.set()
            if not ready_task.done():
                ready_task.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(ready_task, return_exceptions=True)
    result = tmp_path / "one-hundred-streams.json"
    result.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "streams": outcomes,
                "restarts": measurements,
                "qualification": False,
            }
        )
    )
    result.chmod(0o600)
