"""The model store (T018)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_core.models.jobs import ChecksumManifest, ManifestFile
from coire_node.store import Store, StoreError, write_atomic

SLUG = "mlx-community--tiny"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "models")
    s.ensure_root()
    return s


def _populate(store: Store, slug: str = SLUG, files: dict[str, bytes] | None = None) -> Path:
    files = files or {
        "config.json": b'{"quantization": {"bits": 4}}',
        "model.safetensors": b"\x00" * 4096,
        "nested/tokenizer.json": b"{}",
    }
    base = store.path_for(slug)
    for rel, data in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return base


class TestPathSafety:
    """The one choke point that makes FR-017 structural."""

    @pytest.mark.parametrize(
        "bad",
        ["../escape", "/etc/passwd", "a/b", "..", "", "a--b/../..", "./x", "sub/dir"],
    )
    def test_non_slugs_are_refused(self, store: Store, bad: str) -> None:
        with pytest.raises(StoreError):
            store.path_for(bad)

    def test_a_real_slug_resolves_directly_under_the_root(self, store: Store) -> None:
        assert store.path_for(SLUG).parent == store.root

    def test_manifest_and_template_sit_beside_the_copy_not_inside_it(self, store: Store) -> None:
        """Inside would put them in the checksum manifest and make a template change look
        like corruption."""
        assert store.manifest_path(SLUG).parent == store.root
        assert store.template_path(SLUG).parent == store.root
        assert not str(store.manifest_path(SLUG)).startswith(str(store.path_for(SLUG)) + "/")


class TestHashing:
    def test_digests_match_hashlib(self, store: Store) -> None:
        _populate(store)
        manifest = store.hash_tree(SLUG, repo_id="mlx-community/tiny", revision="abc")
        by_path = manifest.by_path()
        expected = hashlib.sha256(b"\x00" * 4096).hexdigest()
        assert by_path["model.safetensors"].sha256 == expected
        assert by_path["model.safetensors"].bytes == 4096

    def test_every_file_including_nested_is_covered(self, store: Store) -> None:
        _populate(store)
        manifest = store.hash_tree(SLUG, repo_id="a/b", revision="r")
        assert set(manifest.by_path()) == {
            "config.json",
            "model.safetensors",
            "nested/tokenizer.json",
        }
        assert manifest.total_bytes == 4096 + 29 + 2

    def test_upstream_digests_are_carried_where_the_hub_gave_one(self, store: Store) -> None:
        """The Hub publishes sha256 for LFS files only; the rest are legitimately null."""
        _populate(store)
        manifest = store.hash_tree(
            SLUG,
            repo_id="a/b",
            revision="r",
            upstream={"model.safetensors": "f" * 64},
        )
        by_path = manifest.by_path()
        assert by_path["model.safetensors"].upstream_sha256 == "f" * 64
        assert by_path["config.json"].upstream_sha256 is None

    def test_progress_is_reported_while_hashing(self, store: Store) -> None:
        """A job that reports nothing for minutes is indistinguishable from a stuck one."""
        _populate(store)
        seen: list[tuple[int, int]] = []
        store.hash_tree(
            SLUG, repo_id="a/b", revision="r", on_progress=lambda n, i: seen.append((n, i))
        )
        assert seen and sum(n for n, _ in seen) == 4096 + 29 + 2

    def test_manifest_round_trips_through_disk(self, store: Store) -> None:
        _populate(store)
        manifest = store.hash_tree(SLUG, repo_id="a/b", revision="r")
        store.write_manifest(manifest)
        loaded = store.read_manifest(SLUG)
        assert loaded is not None and loaded.sha256() == manifest.sha256()

    def test_a_corrupt_manifest_reads_as_absent_rather_than_raising(self, store: Store) -> None:
        _populate(store)
        store.manifest_path(SLUG).write_text("{not json")
        assert store.read_manifest(SLUG) is None


class TestBookkeepingExclusion:
    """`snapshot_download(local_dir=...)` leaves `.cache/huggingface/` inside the copy."""

    def _with_cache(self, store: Store) -> None:
        _populate(store)
        cache = store.path_for(SLUG) / ".cache" / "huggingface" / "download"
        cache.mkdir(parents=True)
        (cache / "model.safetensors.metadata").write_text("origin-only bookkeeping")
        (cache / "model.safetensors.lock").write_text("")

    def test_the_hub_cache_is_not_part_of_the_copy(self, store: Store) -> None:
        self._with_cache(store)
        manifest = store.hash_tree(SLUG, repo_id="a/b", revision="r")
        assert not any(p.startswith(".cache") for p in manifest.by_path())

    def test_a_replica_without_the_cache_still_verifies(self, store: Store, tmp_path: Path) -> None:
        """The real failure this prevents: the origin has bookkeeping, the replica does not,
        so a manifest covering it could never match on the far side."""
        self._with_cache(store)
        origin_manifest = store.hash_tree(SLUG, repo_id="a/b", revision="r")

        replica = Store(tmp_path / "replica")
        replica.ensure_root()
        _populate(replica)  # the model's files only, as an import would write them
        assert replica.verify_against(SLUG, origin_manifest) == []

    def test_total_bytes_excludes_bookkeeping(self, store: Store) -> None:
        self._with_cache(store)
        assert store.hash_tree(SLUG, repo_id="a/b", revision="r").total_bytes == 4096 + 29 + 2


class TestVerification:
    def _manifest(self, store: Store) -> ChecksumManifest:
        _populate(store)
        manifest = store.hash_tree(SLUG, repo_id="a/b", revision="r")
        store.write_manifest(manifest)
        return manifest

    def test_an_untouched_copy_verifies(self, store: Store) -> None:
        manifest = self._manifest(store)
        assert store.verify_against(SLUG, manifest) == []

    def test_a_changed_byte_is_caught(self, store: Store) -> None:
        manifest = self._manifest(store)
        (store.path_for(SLUG) / "model.safetensors").write_bytes(b"\x01" * 4096)
        assert store.verify_against(SLUG, manifest) == ["model.safetensors"]

    def test_a_truncated_file_is_caught(self, store: Store) -> None:
        """The quickstart's failure injection: truncate by one byte."""
        manifest = self._manifest(store)
        target = store.path_for(SLUG) / "model.safetensors"
        target.write_bytes(target.read_bytes()[:-1])
        assert store.verify_against(SLUG, manifest) == ["model.safetensors"]

    def test_a_missing_file_is_caught(self, store: Store) -> None:
        manifest = self._manifest(store)
        (store.path_for(SLUG) / "config.json").unlink()
        assert store.verify_against(SLUG, manifest) == ["config.json"]

    def test_an_extra_file_is_caught(self, store: Store) -> None:
        """A copy that is not the same set of bytes is not a copy."""
        manifest = self._manifest(store)
        (store.path_for(SLUG) / "surprise.bin").write_bytes(b"x")
        assert store.verify_against(SLUG, manifest) == ["surprise.bin"]


class TestDeletion:
    def test_delete_removes_copy_manifest_and_template(self, store: Store) -> None:
        _populate(store)
        store.write_manifest(store.hash_tree(SLUG, repo_id="a/b", revision="r"))
        store.write_template(SLUG, "{{ messages }}")
        assert store.manifest_path(SLUG).exists() and store.template_path(SLUG).exists()

        store.delete(SLUG)
        assert not store.path_for(SLUG).exists()
        assert not store.manifest_path(SLUG).exists()
        assert not store.template_path(SLUG).exists()

    def test_delete_is_idempotent(self, store: Store) -> None:
        """Retirement is driven repeatedly until every node confirms."""
        store.delete(SLUG)
        store.delete(SLUG)

    def test_delete_leaves_other_models_alone(self, store: Store) -> None:
        _populate(store)
        _populate(store, "other--model")
        store.delete(SLUG)
        assert store.exists("other--model")


class TestAtomicWrite:
    def test_a_reader_never_sees_a_partial_file(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        write_atomic(target, b'{"a": 1}')
        write_atomic(target, b'{"a": 2}')
        assert target.read_bytes() == b'{"a": 2}'

    def test_no_temporary_files_are_left_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        write_atomic(target, b"x")
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_a_failed_write_leaves_no_temporary(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import os

        target = tmp_path / "state.json"
        write_atomic(target, b"original")

        def boom(*a: object, **k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            write_atomic(target, b"new")
        assert target.read_bytes() == b"original"
        assert list(tmp_path.iterdir()) == [target]


class TestFreeSpace:
    def test_free_bytes_is_positive(self, store: Store) -> None:
        assert store.free_bytes() > 0


def test_manifest_file_rejects_traversal_even_from_a_peer() -> None:
    """A manifest arrives from another node; its paths are joined to our store root."""
    with pytest.raises(ValueError):
        ChecksumManifest(
            slug=SLUG,
            repo_id="a/b",
            revision="r",
            files=[ManifestFile(path="../../etc/passwd", bytes=1, sha256="0" * 64)],
            total_bytes=1,
            created_at=datetime.now(UTC),
        )
