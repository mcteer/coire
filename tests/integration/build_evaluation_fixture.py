"""Acquire fixed isolated-CI assets through authenticated node acquisition verbs."""

from __future__ import annotations

import platform
import time
import uuid
from pathlib import Path

from coire_node.testing.harness import Agent
from coire_node.testing.training import offline_training_model

REPOSITORIES = (
    "mlx-community/Qwen2.5-Coder-0.5B-Instruct-4bit",
    "mlx-community/Qwen2.5-Coder-1.5B-Instruct-4bit",
)


def main() -> None:
    if platform.node().lower().split(".")[0] == "coire-core":
        raise RuntimeError("CI evaluation acquisition cannot run on core")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("isolated evaluation fixture requires Apple Silicon")
    agent = Agent(
        Path("models/.evaluation-ci-node").resolve(), node_store_dir=str(Path("models").resolve())
    )
    try:
        with agent.client() as client:
            for repo in REPOSITORIES:
                response = client.post("/node/models/inspect", json={"repo_id": repo})
                response.raise_for_status()
                inspection = response.json()
                assert inspection["is_mlx_format"] and inspection["total_bytes"] <= 1024**3
                job_id = uuid.uuid4()
                slug = repo.replace("/", "--")
                response = client.post(
                    "/node/jobs/pull",
                    json={
                        "job_id": str(job_id),
                        "repo_id": repo,
                        "slug": slug,
                        "revision": inspection["revision"],
                        "expected_total_bytes": inspection["total_bytes"],
                    },
                )
                response.raise_for_status()
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    response = client.get(f"/node/jobs/{job_id}")
                    response.raise_for_status()
                    status = response.json()
                    if status["stage"] == "done":
                        break
                    assert status["stage"] not in {"failed", "cancelled"}, status["error_kind"]
                    time.sleep(0.5)
                else:
                    raise RuntimeError("CI acquisition exceeded its deadline")
                offline_training_model(str(agent.store.path_for(slug)))
    finally:
        agent.close()


if __name__ == "__main__":
    main()
