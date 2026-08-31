"""Footprint measurement (T019)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import psutil
import pytest

from coire_node.footprint import phys_footprint, resident_bytes


class TestResidentBytes:
    def test_the_current_process_measures_positive(self) -> None:
        value = resident_bytes(os.getpid())
        assert value is not None and value > 0

    def test_a_dead_process_measures_none_not_zero(self) -> None:
        """An absent measurement is honest; a zero would silently free budget."""
        assert resident_bytes(2**22) is None

    def test_it_tracks_a_large_allocation(self) -> None:
        """The property that matters: the number moves when the process grows."""
        before = resident_bytes(os.getpid())
        ballast = bytearray(64 * 1024 * 1024)
        ballast[::4096] = b"\x01" * len(ballast[::4096])  # touch pages so they are dirty
        after = resident_bytes(os.getpid())
        assert before is not None and after is not None
        assert after - before > 32 * 1024 * 1024
        del ballast


@pytest.mark.skipif(sys.platform != "darwin", reason="phys_footprint is a macOS counter")
class TestDarwin:
    def test_phys_footprint_is_read_and_used(self) -> None:
        pid = os.getpid()
        footprint = phys_footprint(pid)
        assert footprint is not None and footprint > 0
        assert resident_bytes(pid) == footprint

    def test_footprint_and_rss_are_both_plausible(self) -> None:
        """They measure different things and neither dominates in general.

        RSS includes shared clean pages (mapped dylibs, binary text) that footprint excludes,
        so for a small process RSS is larger. Footprint includes IOAccelerator dirty memory
        that RSS misses, which is why an MLX engine inverts the relationship — and why this
        module reads footprint rather than RSS.
        """
        pid = os.getpid()
        footprint = phys_footprint(pid)
        rss = psutil.Process(pid).memory_info().rss
        assert footprint is not None
        assert footprint > 1024 * 1024
        assert rss > 1024 * 1024
        assert footprint < 100 * rss

    def test_a_dead_pid_yields_none(self) -> None:
        assert phys_footprint(2**22) is None


@pytest.mark.skipif(sys.platform == "darwin", reason="the non-Darwin fallback")
def test_non_darwin_falls_back_to_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = os.getpid()
    expected = 158_662_656
    process = psutil.Process(pid)
    monkeypatch.setattr(process, "memory_info", lambda: SimpleNamespace(rss=expected))
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    assert phys_footprint(pid) is None
    assert resident_bytes(pid) == expected
