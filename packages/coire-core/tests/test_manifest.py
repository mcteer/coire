"""Checksum manifests (T009)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from coire_core.models.jobs import (
    ChecksumManifest,
    DownloadStage,
    JobKind,
    JobStage,
    JobStatus,
    ManifestFile,
    next_stage,
)


def _manifest(files: list[ManifestFile], *, created: datetime | None = None) -> ChecksumManifest:
    return ChecksumManifest(
        slug="mlx-community--tiny",
        repo_id="mlx-community/tiny",
        revision="abc123",
        files=files,
        total_bytes=sum(f.bytes for f in files),
        created_at=created or datetime.now(UTC),
    )


A = ManifestFile(path="a.safetensors", bytes=10, sha256="a" * 64)
B = ManifestFile(path="b/config.json", bytes=5, sha256="b" * 64)


class TestCanonicalForm:
    def test_digest_is_independent_of_file_order(self) -> None:
        """Two nodes list a directory in whatever order the filesystem gives them."""
        assert _manifest([A, B]).sha256() == _manifest([B, A]).sha256()

    def test_digest_is_independent_of_creation_time(self) -> None:
        """The replica creates its manifest later than the origin, by definition."""
        later = datetime.now(UTC) + timedelta(hours=3)
        assert _manifest([A, B]).sha256() == _manifest([A, B], created=later).sha256()

    def test_digest_changes_when_a_checksum_changes(self) -> None:
        altered = ManifestFile(path=A.path, bytes=A.bytes, sha256="c" * 64)
        assert _manifest([altered, B]).sha256() != _manifest([A, B]).sha256()

    def test_canonical_bytes_are_compact_and_sorted(self) -> None:
        raw = _manifest([B, A]).canonical_bytes()
        assert b" " not in raw
        assert raw.index(b"a.safetensors") < raw.index(b"b/config.json")


class TestPathSafety:
    @pytest.mark.parametrize(
        "bad", ["../escape", "/absolute", "a/../../b", "", "./x", "a//b", "..", "\\abs"]
    )
    def test_traversing_paths_are_rejected_at_the_boundary(self, bad: str) -> None:
        """A manifest path is joined to the store root on the receiving node."""
        with pytest.raises(ValidationError):
            ManifestFile(path=bad, bytes=1, sha256="0" * 64)

    def test_ordinary_nested_paths_are_accepted(self) -> None:
        assert ManifestFile(path="sub/dir/file.bin", bytes=1, sha256="0" * 64).path

    @pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "0" * 63])
    def test_malformed_digests_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            ManifestFile(path="a", bytes=1, sha256=bad)


class TestStages:
    def test_sequence_advances_in_order(self) -> None:
        assert next_stage(DownloadStage.INSPECT) is DownloadStage.PULL
        assert next_stage(DownloadStage.IMPORT) is DownloadStage.VERIFY_REPLICA
        assert next_stage(DownloadStage.VERIFY_REPLICA) is DownloadStage.DONE

    def test_terminal_stages_are_fixed_points(self) -> None:
        assert next_stage(DownloadStage.DONE) is DownloadStage.DONE
        assert next_stage(DownloadStage.FAILED) is DownloadStage.FAILED


class TestJobStatus:
    def _job(self, **kw: object) -> JobStatus:
        now = datetime.now(UTC)
        base = {
            "job_id": "11111111-1111-1111-1111-111111111111",
            "kind": JobKind.PULL,
            "slug": "a--b",
            "stage": JobStage.TRANSFERRING,
            "started_at": now,
            "updated_at": now,
        }
        return JobStatus(**{**base, **kw})  # type: ignore[arg-type]

    def test_percent_is_bounded_even_when_totals_are_wrong(self) -> None:
        assert self._job(bytes_done=200, bytes_total=100).percent == 100.0

    def test_percent_is_zero_before_a_total_is_known(self) -> None:
        assert self._job(bytes_done=0, bytes_total=0).percent == 0.0

    def test_a_finished_job_with_no_bytes_reads_complete(self) -> None:
        """A verify job moves no bytes; it is still 100% done when it is done."""
        assert self._job(stage=JobStage.DONE, bytes_total=0).percent == 100.0

    def test_terminality(self) -> None:
        assert self._job(stage=JobStage.FAILED).is_terminal
        assert not self._job(stage=JobStage.HASHING).is_terminal
