"""The model store on a Studio.

One flat directory per model under `node_store_dir`, named by the registry's slug, holding
exactly what the repository contained. Plain files, not a Hugging Face cache with symlinks:
an engine is pointed at the directory and has to be able to read it without resolving links
into a cache whose layout is not ours.

Everything that turns an outside string into a path goes through `path_for`, which refuses
anything that is not a slug this platform could have produced. That single choke point is what
makes spec FR-017 structural rather than a rule people have to remember.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from coire_core.models.jobs import ChecksumManifest, ManifestFile
from coire_core.models.registry import is_valid_slug

logger = logging.getLogger(__name__)

HASH_CHUNK_BYTES = 8 * 1024 * 1024
"""8 MiB. Large enough that SHA-256 runs at memory speed, small enough to report progress
often and to keep the worker's resident size flat over a 200 GB tree."""

MANIFEST_SUFFIX = ".manifest.json"
TEMPLATE_SUFFIX = ".chat_template.jinja"

EXCLUDED_DIRS = frozenset({".cache"})
"""Directories inside a copy that are bookkeeping, not content.

`snapshot_download(local_dir=...)` writes `.cache/huggingface/` beside the files it fetches:
lock files, per-file `.metadata`, and a tree listing. They are *per-node* state — different
paths, different mtimes, present on the origin and absent on a replica that received the copy
over the mesh — so hashing them into the manifest would make every replica fail verification
against an origin manifest that can never match. The model's own files are what a copy is."""


class StoreError(RuntimeError):
    """A store operation could not be completed safely."""


def sha256_file(path: Path, *, on_chunk: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
            if on_chunk is not None:
                on_chunk(len(chunk))
    return digest.hexdigest()


class Store:
    """The model store. Paths in, never out."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # -- naming ------------------------------------------------------------
    def path_for(self, slug: str) -> Path:
        """The directory holding one model's files.

        Rejects anything that is not a well-formed slug. `..`, `/`, and absolute paths cannot
        match the slug pattern, so this is the whole of the traversal defence; the resolved
        path is checked against the root as a second, cheap belt.
        """
        if not is_valid_slug(slug):
            raise StoreError(f"not a valid model slug: {slug!r}")
        path = (self.root / slug).resolve()
        if path.parent != self.root:
            raise StoreError(f"slug escapes the store root: {slug!r}")
        return path

    def manifest_path(self, slug: str) -> Path:
        return self.path_for(slug).with_name(slug + MANIFEST_SUFFIX)

    def template_path(self, slug: str) -> Path:
        """Where a chat-template override is written for the engine to read.

        Beside the copy rather than inside it, so it never enters the checksum manifest and a
        template change does not make a verified copy look corrupt.
        """
        return self.path_for(slug).with_name(slug + TEMPLATE_SUFFIX)

    # -- contents ----------------------------------------------------------
    def exists(self, slug: str) -> bool:
        return self.path_for(slug).is_dir()

    def iter_files(self, slug: str) -> Iterator[tuple[str, Path]]:
        """Every file in a copy, as `(relative posix path, absolute path)`, sorted.

        Sorted so a manifest built here and one built on the peer list files in the same order
        before canonicalisation, and so progress is deterministic between runs.
        """
        base = self.path_for(slug)
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            rel = path.relative_to(base)
            if rel.parts and rel.parts[0] in EXCLUDED_DIRS:
                continue
            yield rel.as_posix(), path

    def size_bytes(self, slug: str) -> int:
        return sum(p.stat().st_size for _, p in self.iter_files(slug))

    def free_bytes(self) -> int:
        """Free space on the volume holding the store."""
        self.ensure_root()
        usage = shutil.disk_usage(self.root)
        return usage.free

    # -- manifests ---------------------------------------------------------
    def hash_tree(
        self,
        slug: str,
        *,
        repo_id: str,
        revision: str,
        upstream: dict[str, str | None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ChecksumManifest:
        """Compute a manifest by hashing every file in the copy.

        `on_progress(bytes_delta, files_done)` is called as it goes: hashing a 200 GB tree
        takes minutes and a job that reports nothing for minutes is indistinguishable from a
        stuck one.
        """
        upstream = upstream or {}
        files: list[ManifestFile] = []
        total = 0
        for index, (rel, path) in enumerate(self.iter_files(slug), start=1):
            size = path.stat().st_size
            # `index` is bound now, not when the closure runs: a late-binding lambda inside a
            # loop reports every chunk against the last file.
            callback = (lambda n, i=index: on_progress(n, i)) if on_progress is not None else None
            digest = sha256_file(path, on_chunk=callback)
            files.append(
                ManifestFile(
                    path=rel,
                    bytes=size,
                    sha256=digest,
                    upstream_sha256=upstream.get(rel),
                )
            )
            total += size
        return ChecksumManifest(
            slug=slug,
            repo_id=repo_id,
            revision=revision,
            files=files,
            total_bytes=total,
            created_at=datetime.now(UTC),
        )

    def write_manifest(self, manifest: ChecksumManifest) -> Path:
        """Persist a manifest atomically beside its copy."""
        target = self.manifest_path(manifest.slug)
        write_atomic(target, manifest.model_dump_json(indent=2).encode())
        return target

    def read_manifest(self, slug: str) -> ChecksumManifest | None:
        path = self.manifest_path(slug)
        if not path.is_file():
            return None
        try:
            return ChecksumManifest.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            logger.warning("manifest for %s is unreadable (%s); treating it as absent", slug, exc)
            return None

    def write_template(self, slug: str, template: str) -> Path:
        target = self.template_path(slug)
        write_atomic(target, template.encode())
        return target

    def verify_against(
        self,
        slug: str,
        manifest: ChecksumManifest,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """Recompute checksums and return the paths that do not match.

        An empty list means verified. Missing files, extra files and wrong digests are all
        mismatches: a copy that is *not the same set of bytes* is not a copy.
        """
        expected = manifest.by_path()
        actual = dict(self.iter_files(slug))
        mismatched: list[str] = []

        for index, (rel, entry) in enumerate(sorted(expected.items()), start=1):
            path = actual.get(rel)
            if path is None:
                mismatched.append(rel)
                continue
            if path.stat().st_size != entry.bytes:
                mismatched.append(rel)
                continue
            callback = (lambda n, i=index: on_progress(n, i)) if on_progress is not None else None
            digest = sha256_file(path, on_chunk=callback)
            if digest != entry.sha256:
                mismatched.append(rel)

        mismatched.extend(sorted(set(actual) - set(expected)))
        return sorted(set(mismatched))

    # -- removal -----------------------------------------------------------
    def delete(self, slug: str) -> None:
        """Remove a copy, its manifest, and any chat-template override.

        Idempotent: retirement is driven repeatedly by the reconciler until every node
        confirms, so a second pass must be a no-op rather than an error.
        """
        for path in (self.path_for(slug), self.manifest_path(slug), self.template_path(slug)):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)
        logger.info("deleted local copy %s", slug)


def write_atomic(target: Path, data: bytes) -> None:
    """Write a file so a reader never sees a partial one.

    Temp file in the same directory, then `os.replace`. State files are read by a freshly
    started agent that may have been launched a millisecond after a crash mid-write, and a
    truncated JSON state file is how an agent loses track of a running engine.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_atomic_json(target: Path, payload: object) -> None:
    write_atomic(target, json.dumps(payload, indent=2, default=str).encode())
