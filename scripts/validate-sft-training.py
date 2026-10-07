#!/usr/bin/env python3
"""Explicit authenticated admin/gateway acceptance; never starts a trainer directly."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from openai.types.chat import ChatCompletion
from pydantic import BaseModel

from coire_core.models.adapters import AdapterDetail
from coire_core.models.gateway import ChatCompletionRequest, ChatMessage
from coire_core.models.training import (
    CheckpointPage,
    TrainingCommandReceipt,
    TrainingControlRequest,
    TrainingJobDetail,
    TrainingJobReceipt,
    TrainingMetricPage,
    TrainingSubmission,
    TrainingValidation,
)

JOBS = "/api/v1/admin/training/jobs"


class AcceptanceFailed(RuntimeError):
    """Content-free failure suitable for a private metadata-only execution report."""


class AcceptanceRunner:
    def __init__(
        self, client: httpx.AsyncClient, *, timeout_s: float = 3600, poll_s: float = 1
    ) -> None:
        if not 0 < poll_s <= 5 or not 0 < timeout_s <= 86400:
            raise ValueError("acceptance polling/deadline is outside its bound")
        self.client, self.timeout_s, self.poll_s = client, timeout_s, poll_s
        self.report: dict[str, object] = {
            "schema_version": 1,
            "started_at": datetime.now(UTC).isoformat(),
        }

    async def request[T: BaseModel](
        self,
        method: str,
        path: str,
        response_type: type[T],
        *,
        body: BaseModel | None = None,
        key: str | None = None,
    ) -> T:
        response = await self.client.request(
            method,
            path,
            json=body.model_dump(mode="json") if body is not None else None,
            headers={"Idempotency-Key": key} if key is not None else None,
        )
        if response.is_error:
            # Do not persist raw bodies, YAML validation values or inference text.
            raise AcceptanceFailed(f"api_status_{response.status_code}")
        return response_type.model_validate_json(response.content)

    async def control(
        self, job: TrainingJobDetail, operation: str, key: str
    ) -> TrainingCommandReceipt:
        result = await self.request(
            "POST",
            f"{JOBS}/{job.id}/{operation}",
            TrainingCommandReceipt,
            body=TrainingControlRequest(expected_version=job.version),
            key="sft-control-" + hashlib.sha256(key.encode()).hexdigest(),
        )
        if result.job_id != job.id:
            raise AcceptanceFailed("control_receipt_scope_changed")
        return result

    async def run(
        self,
        submission: TrainingSubmission,
        *,
        key: str,
        pause_at_update: int | None = None,
        cancel_at_update: int | None = None,
    ) -> dict[str, object]:
        if pause_at_update is not None and cancel_at_update is not None:
            raise ValueError("pause and cancel trials must use distinct jobs")
        for threshold in (pause_at_update, cancel_at_update):
            if threshold is not None and threshold < 1:
                raise ValueError("control threshold must be a completed positive update")
        deadline = time.monotonic() + self.timeout_s
        validation = await self.request(
            "POST",
            "/api/v1/admin/training/validate",
            TrainingValidation,
            body=submission,
            key="sft-validate-" + hashlib.sha256(key.encode()).hexdigest(),
        )
        if validation.resolved is None:
            raise AcceptanceFailed("recipe_resolution_pending")
        threshold = pause_at_update if pause_at_update is not None else cancel_at_update
        if threshold is not None and threshold >= validation.spec.optim.updates:
            raise ValueError("control trial needs remaining updates after its threshold")
        receipt = await self.request("POST", JOBS, TrainingJobReceipt, body=submission, key=key)
        self.report["job_id"] = receipt.job_id
        self.report["intent_sha256"] = validation.intent_sha256
        # Prove that an exact retry is the same durable submission, never a second trainer.
        replay = await self.request("POST", JOBS, TrainingJobReceipt, body=submission, key=key)
        if replay.job_id != receipt.job_id:
            raise AcceptanceFailed("submission_replay_created_another_job")
        paused = False
        controlled = False
        control_started = 0.0
        transitions: list[str] = []
        try:
            while time.monotonic() < deadline:
                job = await self.request("GET", f"{JOBS}/{receipt.job_id}", TrainingJobDetail)
                if job.id != receipt.job_id or job.intent_sha256 != validation.intent_sha256:
                    raise AcceptanceFailed("job_intent_scope_changed")
                if job.source_yaml != submission.source_yaml:
                    raise AcceptanceFailed("original_recipe_bytes_changed")
                if not transitions or transitions[-1] != job.state:
                    transitions.append(job.state)
                self.report.update(
                    state=job.state, completed_update=job.completed_update, transitions=transitions
                )
                if job.state in {"failed", "cancelled", "succeeded"}:
                    if cancel_at_update is not None and controlled:
                        if job.state != "cancelled" or job.adapter_id is not None:
                            raise AcceptanceFailed("cancelled_trial_published_output")
                        elapsed = time.monotonic() - control_started
                        self.report["cancel_seconds"] = elapsed
                        if elapsed > 5:
                            raise AcceptanceFailed("healthy_cancel_deadline_exceeded")
                        self.report["passed"] = True
                        return self.report
                    if (
                        job.state != "succeeded"
                        or (pause_at_update is not None and not paused)
                        or (cancel_at_update is not None and not controlled)
                    ):
                        raise AcceptanceFailed("trial_did_not_complete_requested_path")
                    break
                if (
                    controlled
                    and cancel_at_update is not None
                    and time.monotonic() - control_started > 5
                ):
                    raise AcceptanceFailed("healthy_cancel_deadline_exceeded")
                if controlled and pause_at_update is not None and not paused:
                    if job.state == "paused":
                        self.report["pause_seconds"] = time.monotonic() - control_started
                        self.report["resume_update"] = job.completed_update
                        await self.checkpoints(job)
                        await self.control(job, "resume", key + ":resume")
                        paused = True
                    elif time.monotonic() - control_started > 60:
                        raise AcceptanceFailed("pause_deadline_exceeded")
                elif (
                    not controlled
                    and threshold is not None
                    and job.state == "running"
                    and job.completed_update >= threshold
                ):
                    operation = "pause" if pause_at_update is not None else "cancel"
                    control_started = time.monotonic()
                    await self.control(job, operation, key + ":" + operation)
                    controlled = True
                await asyncio.sleep(self.poll_s)
            else:
                raise AcceptanceFailed("acceptance_deadline_exceeded")
            await self.checkpoints(job)
            if job.adapter_id is None or job.completed_update != validation.spec.optim.updates:
                raise AcceptanceFailed("final_output_or_progress_missing")
            adapter = await self.request(
                "GET",
                f"/api/v1/admin/adapters/{job.adapter_id}",
                AdapterDetail,
            )
            if (
                adapter.source_job_id != job.id
                or adapter.state != "ready"
                or adapter.verified
                or adapter.visibility != "admin_only"
                or adapter.base_variant_id != validation.spec.model.variant_id
                or adapter.model_id != validation.spec.model.model_id
            ):
                raise AcceptanceFailed(
                    "adapter_identity_readiness_or_independent_verification_changed"
                )
            # This remains the public authenticated gateway. No engine port/path/argv is accepted.
            result = await self.request(
                "POST",
                "/v1/chat/completions",
                ChatCompletion,
                body=ChatCompletionRequest(
                    model=adapter.selector,
                    coire_variant_id=adapter.base_variant_id,
                    messages=[ChatMessage(role="user", content="Reply with one short word.")],
                    max_tokens=8,
                    temperature=0,
                ),
            )
            if (
                result.model != adapter.selector
                or not result.choices
                or not result.choices[0].message.content
            ):
                raise AcceptanceFailed("exact_adapter_gateway_generation_missing")
            await self.metrics(job)
            self.report.update(
                adapter_id=str(adapter.id),
                selector=adapter.selector,
                base_variant_id=str(adapter.base_variant_id),
                adapter_manifest_sha256=adapter.manifest_sha256,
                source_sha256=job.source_sha256,
                resolved_sha256=adapter.resolved_spec_sha256,
                parameterization=adapter.parameterization,
                placement=validation.spec.placement.mode,
                independent_verification=False,
                passed=True,
                finished_at=datetime.now(UTC).isoformat(),
            )
            return self.report
        except BaseException:
            # Bound leftover work from this explicit trial. Keep uncertainty visible
            # if authorization/connectivity is lost; never infer native death here.
            try:
                current = await self.request("GET", f"{JOBS}/{receipt.job_id}", TrainingJobDetail)
                if current.state not in {"succeeded", "failed", "cancelled"}:
                    await self.control(current, "cancel", key + ":failure-cancel")
                    self.report["cleanup"] = "cancel_requested"
            except Exception:
                self.report["cleanup"] = "unresolved"
            raise

    async def checkpoints(self, job: TrainingJobDetail) -> None:
        page = await self.request("GET", f"{JOBS}/{job.id}/checkpoints", CheckpointPage)
        committed = [item for item in page.items if item.state == "committed"]
        if job.latest_checkpoint_id is None or not any(
            item.id == job.latest_checkpoint_id for item in committed
        ):
            raise AcceptanceFailed("latest_two_copy_checkpoint_missing")
        self.report["checkpoints"] = [
            {
                "id": str(item.id),
                "update": item.update,
                "manifest_sha256": item.manifest_sha256,
                "verified_nodes": item.verified_nodes,
            }
            for item in committed
        ]

    async def metrics(self, job: TrainingJobDetail) -> None:
        cursor = ""
        counts: dict[str, int] = {"train": 0, "validation": 0}
        for _ in range(100):
            path = f"{JOBS}/{job.id}/metrics?limit=2000"
            if cursor:
                path += "&" + str(httpx.QueryParams({"cursor": cursor}))
            page = await self.request("GET", path, TrainingMetricPage)
            for item in page.items:
                if item.job_id != job.id:
                    raise AcceptanceFailed("loss_history_scope_changed")
                if not item.rolled_back:
                    counts[item.kind] += 1
            if page.next_cursor is None:
                break
            if page.next_cursor == cursor:
                raise AcceptanceFailed("loss_history_cursor_did_not_advance")
            cursor = page.next_cursor
        else:
            raise AcceptanceFailed("loss_history_page_bound_exceeded")
        if not all(counts.values()):
            raise AcceptanceFailed("independent_loss_history_missing")
        self.report["loss_samples"] = counts


def write_report(path: Path, report: dict[str, object]) -> None:
    repository = Path(__file__).resolve().parents[1]
    if path.resolve().is_relative_to(repository) or not path.parent.is_dir() or path.is_symlink():
        raise ValueError("acceptance report must use a new external file in an existing directory")
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(report, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--keychain-service")
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--idempotency-key", default="sft-acceptance-" + uuid.uuid4().hex)
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--pause-at-update", type=int)
    controls.add_argument("--cancel-at-update", type=int)
    args = parser.parse_args()
    if os.environ.get("CI"):
        parser.error(
            "real-cluster acceptance is an explicit operator/agent workload, not a CI target"
        )
    url = urlsplit(args.api_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error("API origin must be HTTP(S) without credentials/query/fragment")
    if args.recipe.is_symlink() or not args.recipe.is_file() or args.recipe.stat().st_size > 65536:
        parser.error("recipe must be a bounded local UTF-8 file")
    if (
        args.report.exists()
        or not args.report.parent.is_dir()
        or args.report.resolve().is_relative_to(Path(__file__).resolve().parents[1])
    ):
        parser.error("report must be a new file outside the repository")
    token = os.environ.get("COIRE_API_TOKEN", "")
    if args.keychain_service:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", args.keychain_service],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            parser.error("requested Keychain credential is unavailable")
        token = result.stdout.strip()
    if not token:
        parser.error("COIRE_API_TOKEN or --keychain-service is required")
    submission = TrainingSubmission(
        source_yaml=args.recipe.read_bytes().decode("utf-8"), source_kind="yaml"
    )

    async def execute() -> int:
        async with httpx.AsyncClient(
            base_url=args.api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False,
            trust_env=False,
            timeout=120,
        ) as client:
            runner = AcceptanceRunner(client, timeout_s=args.timeout_seconds)
            runner.report["recipe_file_sha256"] = hashlib.sha256(
                submission.source_yaml.encode()
            ).hexdigest()
            try:
                await runner.run(
                    submission,
                    key=args.idempotency_key,
                    pause_at_update=args.pause_at_update,
                    cancel_at_update=args.cancel_at_update,
                )
            except Exception as error:
                runner.report.update(
                    passed=False,
                    failure=str(error)
                    if isinstance(error, AcceptanceFailed)
                    else type(error).__name__,
                )
                await asyncio.to_thread(write_report, args.report, runner.report)
                print(f"acceptance failed; metadata report: {args.report}")
                return 1
            await asyncio.to_thread(write_report, args.report, runner.report)
            print(f"acceptance passed; metadata report: {args.report}")
            return 0

    return asyncio.run(execute())


if __name__ == "__main__":
    raise SystemExit(main())
