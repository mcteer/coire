"""The acquisition worker: `python -m coire_node.worker <state-file>`.

A separate process, not a thread, for two reasons. The agent reports a hard resource budget
about *itself* (feature 000 FR-012c) and that number has to stay meaningful — hashing 200 GB
inside the agent would blow it and turn `collection_budget_ok` into noise. And a download that
crashes must not take the agent's listeners down with it.

It runs at `nice 10`: the Studios exist to run inference, and an acquisition is background work
by definition.

State is written to the job file after every stage and periodically during transfers, so an
agent that restarts — or a node that reboots — can see exactly where the work got to.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from coire_core.models.jobs import (
    ChecksumManifest,
    JobErrorKind,
    JobKind,
    JobStage,
    JobStatus,
)
from coire_core.net import DataFabricClient, FabricUnreachable
from coire_node.store import Store, write_atomic

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger("coire_node.worker")

PROGRESS_INTERVAL_BYTES = 64 * 1024 * 1024
"""Write progress at most this often. Frequent enough that a stalled job is visible within
seconds, rare enough that a 200 GB transfer does not spend its time on fsync."""

DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
NICE_INCREMENT = 10

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


class JobFile:
    """The job's state on disk. The single channel between worker and agent."""

    def __init__(self, path: Path) -> None:
        self.path = path
        raw = json.loads(path.read_text())
        self.params: dict[str, Any] = raw.get("params", {})
        self.status = JobStatus.model_validate(raw["status"])
        self._last_write = 0.0
        self._last_bytes = 0

    def save(self, *, force: bool = False) -> None:
        if not force and self.status.bytes_done - self._last_bytes < PROGRESS_INTERVAL_BYTES:
            return
        self.status.updated_at = datetime.now(UTC)
        write_atomic(
            self.path,
            json.dumps(
                {"params": self.params, "status": self.status.model_dump(mode="json")},
                indent=2,
            ).encode(),
        )
        self._last_write = time.monotonic()
        self._last_bytes = self.status.bytes_done

    def fail(
        self, kind: JobErrorKind, message: str, *, mismatched: list[str] | None = None
    ) -> None:
        self.status.stage = JobStage.FAILED
        self.status.error = message[:2000]
        self.status.error_kind = kind
        self.status.mismatched_paths = mismatched or []
        self.status.finished_at = datetime.now(UTC)
        self.save(force=True)
        logger.error("job %s failed (%s): %s", self.status.job_id, kind.value, message)

    def finish(self, manifest: ChecksumManifest | None = None) -> None:
        self.status.stage = JobStage.DONE
        self.status.finished_at = datetime.now(UTC)
        if manifest is not None:
            self.status.manifest = manifest
            self.status.manifest_sha256 = manifest.sha256()
        self.save(force=True)
        logger.info("job %s done", self.status.job_id)


def _store(job: JobFile) -> Store:
    store = Store(job.params["store_dir"])
    store.ensure_root()
    return store


# --------------------------------------------------------------------------- pull


def run_pull(job: JobFile) -> int:
    """Download from Hugging Face, then hash what arrived."""
    from coire_node import hub

    store = _store(job)
    slug = job.status.slug
    repo_id = job.params["repo_id"]
    revision = job.params.get("revision", "main")
    token = os.environ.get("HF_TOKEN") or None

    job.status.stage = JobStage.RESOLVING
    job.save(force=True)
    try:
        inspection = hub.inspect(
            repo_id, revision=revision, token=token, cache_dir=job.params.get("cache_dir")
        )
    except hub.HubError as exc:
        job.fail(exc.kind, str(exc))
        return EXIT_FAILED

    job.status.bytes_total = inspection.total_bytes
    job.status.files_total = len(inspection.files)
    job.status.stage = JobStage.TRANSFERRING
    job.save(force=True)

    required = inspection.total_bytes + int(job.params.get("disk_reserve_bytes", 0))
    if store.free_bytes() < required:
        job.fail(
            JobErrorKind.DISK_FULL,
            f"needs {required} bytes including the reserve, {store.free_bytes()} free",
        )
        return EXIT_FAILED

    try:
        hub.snapshot(
            repo_id,
            revision=inspection.revision,
            local_dir=store.path_for(slug),
            token=token,
        )
    except hub.HubError as exc:
        # The partial download is deliberately kept: completed files are reused on retry
        # (research R5, per-file resume).
        job.fail(exc.kind, str(exc))
        return EXIT_FAILED

    job.status.stage = JobStage.HASHING
    job.status.bytes_done = 0
    job.save(force=True)

    upstream = {f.path: f.upstream_sha256 for f in inspection.files}
    manifest = store.hash_tree(
        slug,
        repo_id=repo_id,
        revision=inspection.revision,
        upstream=upstream,
        on_progress=lambda n, i: _progress(job, n, i),
    )

    # Verify what we received against what Hugging Face said it would be. This is what makes
    # the origin copy *verified* rather than merely present, and it is the only check that can
    # catch corruption introduced between the Hub and this disk.
    mismatched = [
        f.path for f in manifest.files if f.upstream_sha256 and f.upstream_sha256 != f.sha256
    ]
    if mismatched:
        store.delete(slug)
        job.fail(
            JobErrorKind.CHECKSUM_MISMATCH,
            "downloaded files do not match the digests Hugging Face published",
            mismatched=mismatched,
        )
        return EXIT_FAILED

    store.write_manifest(manifest)
    job.status.files_done = len(manifest.files)
    job.finish(manifest)
    return EXIT_OK


# --------------------------------------------------------------------------- import


def run_import(job: JobFile) -> int:
    """Fetch a copy from the origin over the mesh and verify it file by file."""
    store = _store(job)
    slug = job.status.slug
    source = job.params["source_node"]
    grant = job.params["grant"]
    port = int(job.params.get("node_data_port", 9401))
    manifest = ChecksumManifest.model_validate(job.params["manifest"])

    job.status.bytes_total = manifest.total_bytes
    job.status.files_total = len(manifest.files)
    job.status.stage = JobStage.TRANSFERRING
    job.save(force=True)

    required = manifest.total_bytes + int(job.params.get("disk_reserve_bytes", 0))
    if store.free_bytes() < required:
        job.fail(JobErrorKind.DISK_FULL, f"needs {required} bytes, {store.free_bytes()} free")
        return EXIT_FAILED

    base = store.path_for(slug)
    base.mkdir(parents=True, exist_ok=True)

    # The whole transfer runs inside one event loop. An httpx.AsyncClient is bound to the loop
    # that created it, so a loop per file — the obvious shape, since each file is one request —
    # fails on the second file with "Event loop is closed".
    try:
        asyncio.run(_fetch_all(job, store, slug, source, port, grant, manifest))
    except FabricUnreachable as exc:
        job.fail(JobErrorKind.NETWORK, f"the origin is unreachable over the data fabric: {exc}")
        return EXIT_FAILED
    except httpx.HTTPError as exc:
        job.fail(JobErrorKind.NETWORK, f"transfer failed: {exc}")
        return EXIT_FAILED

    job.status.stage = JobStage.HASHING
    job.save(force=True)
    mismatched = store.verify_against(slug, manifest)
    if mismatched:
        # The partial copy is removed rather than retried in place (spec FR-009): a directory
        # that failed verification is not a starting point, it is evidence.
        store.delete(slug)
        job.fail(
            JobErrorKind.CHECKSUM_MISMATCH,
            "the replicated copy does not match the origin's manifest",
            mismatched=mismatched,
        )
        return EXIT_FAILED

    store.write_manifest(manifest)
    job.status.files_done = len(manifest.files)
    job.finish(manifest)
    return EXIT_OK


async def _fetch_all(
    job: JobFile,
    store: Store,
    slug: str,
    source: str,
    port: int,
    grant: str,
    manifest: ChecksumManifest,
) -> None:
    """Fetch every file of a copy over the mesh, resuming what is already there."""
    base = store.path_for(slug)
    # fallback=False: replication may not cross the egress interface (spec FR-007, SC-004).
    async with DataFabricClient(timeout=120.0) as client:
        for index, entry in enumerate(sorted(manifest.files, key=lambda f: f.path), start=1):
            target = base / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size == entry.bytes:
                job.status.bytes_done += entry.bytes
                job.status.files_done = index
                job.save()
                continue
            await _fetch_file(job, client, source, port, grant, entry.path, target, index)


async def _fetch_file(
    job: JobFile,
    client: DataFabricClient,
    source: str,
    port: int,
    grant: str,
    rel_path: str,
    target: Path,
    index: int,
) -> None:
    """Stream one file, resuming from whatever is already on disk."""
    partial = target.with_suffix(target.suffix + ".part")
    have = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}

    async with client.stream(
        "GET",
        source,
        f"/node/export/{grant}/files/{rel_path}",
        port=port,
        headers=headers,
    ) as response:
        if response.status_code not in (200, 206):
            await response.aread()
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code} fetching {rel_path}",
                request=response.request,
                response=response,
            )
        # 200 to a Range request means the server ignored it: start the file over rather than
        # appending a whole file onto a partial one.
        mode = "ab" if response.status_code == 206 and have else "wb"
        with partial.open(mode) as handle:
            async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                handle.write(chunk)
                job.status.bytes_done += len(chunk)
                job.save()

    partial.replace(target)
    job.status.files_done = index
    job.save(force=True)


# --------------------------------------------------------------------------- verify


def run_verify(job: JobFile) -> int:
    store = _store(job)
    slug = job.status.slug
    supplied = job.params.get("manifest")
    manifest = ChecksumManifest.model_validate(supplied) if supplied else store.read_manifest(slug)
    if manifest is None:
        job.fail(JobErrorKind.NOT_FOUND, f"no manifest for {slug} and none supplied")
        return EXIT_FAILED

    job.status.stage = JobStage.HASHING
    job.status.bytes_total = manifest.total_bytes
    job.status.files_total = len(manifest.files)
    job.save(force=True)

    mismatched = store.verify_against(slug, manifest, on_progress=lambda n, i: _progress(job, n, i))
    if mismatched:
        job.fail(
            JobErrorKind.CHECKSUM_MISMATCH,
            f"{len(mismatched)} file(s) do not match the manifest",
            mismatched=mismatched,
        )
        return EXIT_FAILED
    job.finish(manifest)
    return EXIT_OK


def _progress(job: JobFile, delta: int, files_done: int) -> None:
    job.status.bytes_done += delta
    job.status.files_done = files_done
    job.save()


# --------------------------------------------------------------------------- entry point


RUNNERS = {JobKind.PULL: run_pull, JobKind.IMPORT: run_import, JobKind.VERIFY: run_verify}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m coire_node.worker <state-file>", file=sys.stderr)
        return EXIT_USAGE

    try:
        os.nice(NICE_INCREMENT)
    except OSError:  # pragma: no cover - permitted to fail; the work still runs
        logger.warning("could not lower priority; acquisition will compete with inference")

    job = JobFile(Path(args[0]))
    logger.info("worker starting: %s %s", job.status.kind.value, job.status.slug)
    try:
        return RUNNERS[job.status.kind](job)
    except KeyboardInterrupt:
        job.fail(JobErrorKind.CANCELLED, "cancelled")
        return EXIT_FAILED
    except Exception as exc:
        logger.exception("worker crashed")
        job.fail(JobErrorKind.INTERNAL, f"{type(exc).__name__}: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
