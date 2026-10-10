"""Isolated CI acquisition and node-owned conversion of both tiny preference bases."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import build_evaluation_fixture as acquisition

from coire_node.testing.harness import Agent
from coire_node.testing.training import offline_training_model

REPOSITORY = "mlx-community/Qwen2.5-Coder-0.5B-Instruct-4bit"
QUANTIZED = REPOSITORY.replace("/", "--")
DENSE_PRECISION = "bf16"
DENSE = QUANTIZED + ".preference-dense-" + DENSE_PRECISION
CONVERSION_MEMORY_BYTES = 3 * 1024**3


def main() -> None:
    # The existing helper refuses core/non-Apple hosts before assembling a node.
    acquisition.REPOSITORIES = (REPOSITORY,)
    acquisition.main()
    agent = Agent(
        Path("models/.preference-ci-node").resolve(), node_store_dir=str(Path("models").resolve())
    )
    reservation_id: str | None = None
    try:
        with agent.client() as client:
            manifest = agent.store.read_manifest(QUANTIZED)
            assert manifest is not None
            response = client.post(
                "/node/jobs/reservations",
                json={
                    "idempotency_key": str(uuid.uuid4()),
                    "workflow_id": str(uuid.uuid4()),
                    "variant_id": str(uuid.uuid4()),
                    "memory_bytes": CONVERSION_MEMORY_BYTES,
                    "disk_bytes": 3 * 1024**3,
                },
            )
            response.raise_for_status()
            reservation_id = response.json()["id"]
            job_id = uuid.uuid4()
            response = client.post(
                "/node/jobs/convert",
                json={
                    "job_id": str(job_id),
                    "repo_id": manifest.repo_id,
                    "revision": manifest.revision,
                    "source_slug": QUANTIZED,
                    "target_slug": DENSE,
                    "reservation_id": reservation_id,
                    "recipe": {
                        "name": "preference-dense-" + DENSE_PRECISION,
                        "precision": DENSE_PRECISION,
                    },
                    "dequantize": True,
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
                raise RuntimeError("isolated preference conversion exceeded its deadline")
            offline_training_model(str(agent.store.path_for(DENSE)))
    finally:
        if reservation_id is not None:
            with agent.client() as client:
                client.delete(f"/node/jobs/reservations/{reservation_id}").raise_for_status()
        agent.close()


if __name__ == "__main__":
    main()
