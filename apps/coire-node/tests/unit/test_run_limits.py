from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from coire_core.settings import Settings
from coire_node.runs import RunManager


class Docker:
    def __init__(self) -> None:
        self.killed = False

    async def inspect_container(self, _: str) -> dict[str, object]:
        return {
            "Id": "container",
            "Config": {"Labels": {"com.coire.timeout-seconds": "10"}},
            "State": {
                "Status": "exited" if self.killed else "running",
                "ExitCode": 137 if self.killed else 0,
                "StartedAt": (datetime.now(UTC) - timedelta(seconds=11)).isoformat(),
                "FinishedAt": datetime.now(UTC).isoformat() if self.killed else "",
            },
        }

    async def wait_container(self, _: str) -> int:
        await asyncio.sleep(60)
        return 0

    async def kill_container(self, _: str) -> None:
        self.killed = True

    async def stats(self, _: str) -> dict[str, object]:
        return {
            "memory_stats": {"max_usage": 123},
            "cpu_stats": {"cpu_usage": {"total_usage": 456}},
        }


async def test_wall_clock_limit_kills_and_records_distinct_state() -> None:
    docker = Docker()
    manager = RunManager(
        Settings(_secrets_dir="/none"),  # type: ignore[call-arg]
        docker,  # type: ignore[arg-type]
    )
    result = await manager.wait(uuid.uuid4())
    assert result.state == "timed_out"
    assert result.exit_code == 137
    assert result.resource_usage.peak_memory_bytes == 123
    assert result.resource_usage.cpu_nanoseconds == 456
