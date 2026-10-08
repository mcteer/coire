"""Offline real generation on an isolated Apple Silicon node, never on core.

These tests measure execution completeness, identities and strict evidence, not
model quality. The release gate separately exercises authenticated Studio jobs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from coire_agent.evaluation import execute_phase
from coire_core.evaluation_suites import template
from coire_core.models.engine import EngineState
from coire_core.models.evaluation import (
    EvaluationGeneration,
    EvaluationOutput,
    EvaluationWorkerResult,
    EvaluationWorkload,
    TemplateId,
    canonical_digest,
)
from coire_node.testing.harness import Agent
from coire_node.testing.training import offline_training_model

pytestmark = [pytest.mark.engine, pytest.mark.integration]
FIXTURE = Path(__file__).resolve().parent / "fixtures/evaluations/workload.json"


@pytest.fixture(scope="module")
def evaluation_engine(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Agent, Path, Path]]:
    if os.environ.get("COIRE_EVALUATION_ENGINE") != "1":
        pytest.skip("opt in to the isolated offline evaluation engine gate")
    # This guard runs before construction/spawn or any model-library import.
    candidate = offline_training_model(os.environ.get("COIRE_TEST_MODEL"))
    judge = offline_training_model(os.environ.get("COIRE_TEST_JUDGE"))
    assert candidate.parent == judge.parent
    assert candidate != judge
    candidate_manifest = candidate.with_name(candidate.name + ".manifest.json").read_bytes()
    judge_manifest = judge.with_name(judge.name + ".manifest.json").read_bytes()
    assert hashlib.sha256(candidate_manifest).digest() != hashlib.sha256(judge_manifest).digest()
    agent = Agent(
        tmp_path_factory.mktemp("evaluation-node"),
        node_store_dir=str(candidate.parent),
        node_engine_start_timeout_s=120,
    )
    try:
        yield agent, candidate, judge
    finally:
        agent.close()


async def phase(
    agent: Agent,
    model: Path,
    template_id: TemplateId,
    *,
    previous: list[EvaluationOutput] | None = None,
    workload: EvaluationWorkload | None = None,
) -> tuple[EvaluationWorkload, EvaluationWorkerResult]:
    engine_id = uuid.uuid4()
    original = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    manifest = agent.store.read_manifest(model.name)
    assert manifest is not None
    model_id = uuid.uuid5(uuid.NAMESPACE_URL, manifest.repo_id)
    identity = original.target.target.model_copy(
        update={
            "model_id": model_id,
            "variant_id": uuid.uuid5(model_id, manifest.sha256()),
            "base_manifest_sha256": manifest.sha256(),
        }
    )
    if workload is not None:
        identity = workload.target.target
        assert identity.base_manifest_sha256 == manifest.sha256()
    _, status = agent.engines.start(
        engine_id=engine_id, slug=model.name, estimate_bytes=2 * 1024**3, target=identity
    )
    try:
        deadline = asyncio.get_running_loop().time() + 120
        while status.state is not EngineState.READY:
            assert status.state is not EngineState.FAILED
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.25)
            current = agent.engines.get(engine_id)
            assert current is not None
            status = current
        engine_version = agent.engines.attested_engine_version(engine_id, identity)
        assert engine_version and engine_version != "unknown"
        original = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
        manifest = agent.store.read_manifest(model.name)
        assert manifest is not None
        runtime = original.target.runtime.model_copy(
            update={
                "engine_version": engine_version,
                "runtime_sha256": hashlib.sha256(engine_version.encode()).hexdigest(),
                "tokenizer_sha256": hashlib.sha256(
                    (model / "tokenizer.json").read_bytes()
                ).hexdigest(),
                "template_sha256": hashlib.sha256(
                    (model / "tokenizer_config.json").read_bytes()
                ).hexdigest(),
                "capability_sha256": canonical_digest(original.target.capability_profile),
            }
        )
        model_id = uuid.uuid5(uuid.NAMESPACE_URL, manifest.repo_id)
        target = original.target.model_copy(
            update={
                "runtime": runtime,
                "variant_slug": model.name,
                "public_selector": model_id,
                "target": original.target.target.model_copy(
                    update={
                        "model_id": model_id,
                        "variant_id": uuid.uuid5(model_id, manifest.sha256()),
                        "base_manifest_sha256": manifest.sha256(),
                    }
                ),
            }
        )
        installed = template(template_id)
        suite = original.suite.model_copy(
            update={
                "template": installed,
                "judge": target if template_id.startswith("judge-") else None,
            }
        )
        frozen = suite.model_dump(
            mode="json",
            include={
                "suite_id",
                "version",
                "template",
                "generation",
                "timeout_seconds",
                "judge",
                "judge_generation",
            },
        )
        suite = suite.model_copy(
            update={
                "content_sha256": hashlib.sha256(
                    json.dumps(
                        frozen,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            }
        )
        work = original.model_copy(
            update={
                "attempt_id": uuid.uuid4(),
                "run_id": uuid.uuid4(),
                "deadline": datetime.now(UTC) + timedelta(minutes=15),
                "phase": "judge"
                if previous is not None
                else "harness"
                if template_id == "harness-capability"
                else "base",
                "target": target,
                "suite": suite,
                "previous_outputs": previous or [],
            }
        )
        work = EvaluationWorkload.model_validate(work.model_dump(mode="json"))
        if workload is not None:
            assert workload.target.runtime == runtime
            assert workload.target.variant_slug == model.name
            work = workload
        calls = 0
        statuses: list[int] = []
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:

            async def generate(
                system: str, prompt: str, generation: EvaluationGeneration
            ) -> tuple[str, int, int]:
                nonlocal calls
                response = await client.post(
                    f"http://127.0.0.1:{status.port}/v1/chat/completions",
                    json={
                        "model": str(model),
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": generation.temperature,
                        "top_p": generation.top_p,
                        "seed": generation.seed,
                        "max_tokens": generation.max_tokens,
                    },
                )
                statuses.append(response.status_code)
                response.raise_for_status()
                body = response.json()
                calls += 1
                return (
                    body["choices"][0]["message"]["content"],
                    body["usage"]["prompt_tokens"],
                    body["usage"]["completion_tokens"],
                )

            result = await execute_phase(work, generate, runtime=runtime, prior_outputs=previous)
        assert calls > 0, (statuses, result.reason)
        assert result.request_sha256 == canonical_digest(work)
        assert result.runtime.engine_version == engine_version
        assert result.suite_sha256 == work.suite.content_sha256
        return work, result
    finally:
        stopped = agent.engines.stop(engine_id)
        assert stopped is not None
        deadline = asyncio.get_running_loop().time() + 15
        while stopped.state is not EngineState.STOPPED:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.1)
            current = agent.engines.get(engine_id)
            assert current is not None
            stopped = current
