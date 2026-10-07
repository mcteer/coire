"""Retired attempt erasure with fresh group-death proof and durable replay receipts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import (
    TrainingAttemptCleanupReceipt,
    TrainingAttemptCleanupRequest,
    TrainingPrepareRequest,
)
from coire_node.training.artifacts import TrainingArtifacts

if TYPE_CHECKING:
    from coire_node.training.supervisor import TrainingSupervisor

tracer = trace.get_tracer("coire.node.training")
outcomes = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_attempt_cleanup_total"
)


def cleanup_attempt(
    supervisor: TrainingSupervisor,
    request: TrainingAttemptCleanupRequest,
) -> TrainingAttemptCleanupReceipt:
    request = TrainingAttemptCleanupRequest.model_validate(request.model_dump(mode="json"))
    digest = hashlib.sha256(
        json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    journal = supervisor.journal
    with journal.lock, tracer.start_as_current_span("coire.node.training.attempt.cleanup") as span:
        span.set_attribute("coire.job_id", request.job_id)
        span.set_attribute("coire.attempt_id", request.attempt_id)
        record = journal.get(request.attempt_id)
        prepared = TrainingPrepareRequest.model_validate(record["prepare"])
        if (
            request.job_id != prepared.job_id
            or request.node != prepared.node
            or request.fence != prepared.fence
            or request.prepared_command_id != prepared.command_id
        ):
            raise TrainingConflict("Attempt cleanup scope differs from its immutable preparation")
        previous = record.get("cleanup")
        if previous is not None:
            if previous["digest"] != digest:
                raise TrainingConflict("Attempt cleanup intent changed")
            if previous.get("receipt") is not None:
                return TrainingAttemptCleanupReceipt.model_validate(previous["receipt"])
        else:
            observed = supervisor.observe(request.attempt_id)
            if observed.liveness != "stopped" or not journal.get(request.attempt_id)["released"]:
                raise TrainingConflict(
                    "Attempt cleanup requires current death and released memory proof"
                )
            if supervisor.preparations.get(request.attempt_id):
                raise TrainingConflict("Attempt inputs are still owned")
            # Common checkpoint bundles and all rank components must be erased by
            # their exact-copy retirement commands first. Serving adapters have
            # independent ownership/retention and are never removed here.
            artifacts = TrainingArtifacts(supervisor.artifact_root, node_name=prepared.node)
            for path in supervisor.artifact_root.iterdir():
                if not path.is_dir() or path.name.startswith("."):
                    continue
                manifest = artifacts.manifest(uuid.UUID(path.name))
                if manifest.kind == "checkpoint" and manifest.attempt_id == request.attempt_id:
                    raise TrainingConflict("Checkpoint bytes still reference this attempt")
            if any(
                path.name.startswith(".rank-components-")
                for path in supervisor.artifact_root.iterdir()
            ):
                # Unknown/unfinished rank ownership must not become uncounted.
                raise TrainingConflict("Rank component cleanup remains unresolved")
            for path in supervisor.artifact_root.iterdir():
                if path.is_dir() and path.name.startswith((".checkpoint-", ".rank-", ".import-")):
                    raise TrainingConflict("Artifact staging ownership remains unresolved")
            with journal.transaction():
                record = journal.get(request.attempt_id)
                record["cleanup"] = {
                    "digest": digest,
                    "request": request.model_dump(mode="json"),
                    "receipt": None,
                }
                journal.save(record)
        directory = journal.root / request.attempt_id
        hidden = journal.root / f".cleanup-{request.attempt_id}"
        if directory.exists() or directory.is_symlink():
            TrainingArtifacts._contained_tree(directory)
            if hidden.exists() or hidden.is_symlink():
                raise TrainingConflict("Attempt cleanup has competing staging")
            os.rename(directory, hidden)
        if hidden.exists() or hidden.is_symlink():
            TrainingArtifacts._contained_tree(hidden)
            shutil.rmtree(hidden)
        fd = os.open(journal.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        receipt = TrainingAttemptCleanupReceipt(
            command_id=request.command_id,
            job_id=request.job_id,
            attempt_id=request.attempt_id,
            fence=request.fence,
            node=request.node,
            purged=True,
        )
        with journal.transaction():
            record = journal.get(request.attempt_id)
            journal.db.execute("DELETE FROM events WHERE attempt=?", (request.attempt_id,))
            record["disk_bytes"] = 0
            record["input_bytes"] = 0
            record["input_holds"] = {}
            record.pop("input_files", None)
            record.pop("input_sources", None)
            record["cleanup"]["receipt"] = receipt.model_dump(mode="json")
            journal.save(record)
        outcomes.add(1, {"outcome": "purged"})
        return receipt
