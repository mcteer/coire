"""Studio-only inert tokenizer inspection with fixed CPU, RSS and time bounds."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import resource
import sys
import threading
import time
from importlib.metadata import version
from pathlib import Path

import psutil

from coire_core.models.evaluation import (
    EvaluationIdentityRequest,
    EvaluationProbePrepare,
    EvaluationProbePrepared,
    EvaluationProbePrompt,
    EvaluationRuntime,
    canonical_digest,
)
from coire_core.models.registry import EngineBackend


def engine_runtime_version(backend: EngineBackend) -> str:
    package = {EngineBackend.MLX_LM: "mlx-lm", EngineBackend.MLX_VLM: "mlx-vlm"}[backend]
    return version(package)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["identity", "probes"])
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--agent-image", required=True)
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.node().lower().split(".", 1)[0] == "coire-core":
        return 2
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = "1"
    stopped = threading.Event()
    deadline = time.monotonic() + 85
    process = psutil.Process()

    def guard() -> None:
        while not stopped.wait(0.25):
            try:
                if time.monotonic() >= deadline or process.memory_info().rss > 1024**3:
                    os._exit(124)
            except psutil.Error:
                os._exit(124)

    threading.Thread(target=guard, daemon=True).start()
    try:
        from coire_node.training.datasets import load_evaluation_tokenizer

        payload = sys.stdin.buffer.read(1024**2 + 1)
        if len(payload) > 1024**2:
            return 2
        if args.mode == "identity":
            body = EvaluationIdentityRequest.model_validate_json(payload)
            _, tokenizer_sha, template_sha, runtime_sha = load_evaluation_tokenizer(
                args.store_root / body.variant_slug,
                base_manifest_sha256=body.target.base_manifest_sha256,
                template_override=body.template_override,
            )
            result = EvaluationRuntime(
                engine_version=engine_runtime_version(body.engine_backend),
                harness_version="0.1.0",
                runtime_sha256=hashlib.sha256(
                    (runtime_sha + args.agent_image).encode()
                ).hexdigest(),
                tokenizer_sha256=tokenizer_sha,
                template_sha256=template_sha,
                capability_sha256=canonical_digest(body.capability_profile),
            )
            sys.stdout.write(result.model_dump_json())
        else:
            from coire_core.evaluation_suites.measurement import PROMPTS, prompt_digest

            probe = EvaluationProbePrepare.model_validate_json(payload)
            if probe.prompt_set_sha256 != prompt_digest():
                return 2
            counts: list[dict[object, int]] = [{} for _ in PROMPTS]
            for target in probe.targets:
                tokenizer, *_ = load_evaluation_tokenizer(
                    args.store_root / target.identity.variant_slug,
                    base_manifest_sha256=target.identity.target.base_manifest_sha256,
                    template_override=target.identity.template_override,
                )
                for index, prompt in enumerate(PROMPTS):
                    counts[index][target.instance_id] = len(
                        tokenizer.apply_chat_template(
                            [{"role": "user", "content": prompt}],
                            tools=None,
                            tokenize=True,
                            add_generation_prompt=True,
                            enable_thinking=True,
                        )
                    )
            prepared = EvaluationProbePrepared.model_validate(
                {
                    "measurement_id": probe.measurement_id,
                    "prompt_set_sha256": probe.prompt_set_sha256,
                    "prompts": [
                        EvaluationProbePrompt.model_validate(
                            {
                                "id": f"probe-{index}",
                                "text": prompt,
                                "tokens_by_instance": counts[index],
                            }
                        )
                        for index, prompt in enumerate(PROMPTS)
                    ],
                }
            )
            sys.stdout.write(prepared.model_dump_json())
        return 0
    except Exception:
        return 2
    finally:
        stopped.set()


if __name__ == "__main__":
    raise SystemExit(main())
