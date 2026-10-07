"""Node metrics unit tests (T050)."""

from __future__ import annotations

from typing import Any

import pytest

from coire_core.models.node import NodePath, ThermalState
from coire_node import metrics as m


def collector(**overrides: Any) -> m.MetricsCollector:
    kwargs: dict[str, Any] = {
        "node_name": "coire-edge-a",
        "agent_version": "0.1.0",
        "interval_s": 10.0,
        "budget_cpu_pct": 2.0,
        "budget_rss_bytes": 150 * 1024 * 1024,
    }
    kwargs.update(overrides)
    return m.MetricsCollector(**kwargs)


def test_sample_populates_every_contract_field() -> None:
    status = collector().sample()
    assert status.name == "coire-edge-a"
    assert status.memory_total_bytes > 0
    assert status.disk_total_bytes > 0
    assert 0 <= status.cpu_percent <= 100
    assert status.agent_rss_bytes > 0
    assert status.path is NodePath.MESH


def test_node_status_counts_disjoint_engine_image_and_acquisition_holds() -> None:
    class Engines:
        def committed_bytes(self) -> int:
            return 100

        def budget_bytes(self) -> int:
            return 1000

        def statuses(self) -> list[object]:
            return []

    class Images:
        def committed_bytes(self) -> int:
            return 50

        def measured_resident_bytes(self) -> int:
            return 40

    class Reservations:
        def held_bytes(self) -> int:
            return 25

    class Jobs:
        def active(self) -> list[object]:
            return []

    class Store:
        def free_bytes(self) -> int:
            return 1000

    c = collector()
    c.attach(
        store=Store(),
        jobs=Jobs(),
        engines=Engines(),
        image_workers=Images(),
        reservations=Reservations(),
    )
    status = c.sample()
    assert status.memory_budget_bytes == 1000
    assert status.memory_committed_bytes == 175
    assert status.image_worker_resident_bytes == 40

    def lost_image_hold() -> int:
        raise RuntimeError("image journal unreadable")

    images = Images()
    images.committed_bytes = lost_image_hold  # type: ignore[method-assign]
    c.attach(
        store=Store(),
        jobs=Jobs(),
        engines=Engines(),
        image_workers=images,
        reservations=Reservations(),
    )
    assert c.sample().memory_committed_bytes == 1000


def test_gpu_percent_is_none_when_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing GPU reading must be honest, not fabricated as zero."""
    monkeypatch.setattr("coire_node.metrics.shutil.which", lambda _: None)
    assert m.read_gpu_percent() is None


def test_gpu_percent_parses_ioreg_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class Out:
        stdout = b'"Device Utilization %"=41\n  "Device Utilization %" = 73\n'

    monkeypatch.setattr("coire_node.metrics.shutil.which", lambda _: "/usr/sbin/ioreg")
    monkeypatch.setattr("coire_node.metrics.run_probe", lambda *a, **k: Out())
    assert m.read_gpu_percent() == 73.0


def test_gpu_percent_survives_ioreg_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coire_node.metrics.shutil.which", lambda _: "/usr/sbin/ioreg")

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("ioreg exploded")

    monkeypatch.setattr("coire_node.metrics.run_probe", boom)
    assert m.read_gpu_percent() is None


def test_thermal_state_unknown_when_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coire_node.metrics.shutil.which", lambda _: None)
    monkeypatch.setattr(m, "_read_process_thermal_level", lambda: None)
    assert m.read_thermal_state() is ThermalState.UNKNOWN


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (0, ThermalState.NOMINAL),
        (1, ThermalState.FAIR),
        (2, ThermalState.SERIOUS),
        (3, ThermalState.CRITICAL),
        (4, ThermalState.UNKNOWN),
        (None, ThermalState.UNKNOWN),
    ],
)
def test_public_thermal_api_when_ioreg_field_is_absent(
    monkeypatch: pytest.MonkeyPatch, level: int | None, expected: ThermalState
) -> None:
    class Out:
        stdout = b'"IOPMrootDomain" = {}'

    monkeypatch.setattr("coire_node.metrics.shutil.which", lambda _: "/usr/sbin/ioreg")
    monkeypatch.setattr("coire_node.metrics.run_probe", lambda *a, **k: Out())
    monkeypatch.setattr(m, "_read_process_thermal_level", lambda: level)
    assert m.read_thermal_state() is expected


def test_public_thermal_api_load_failure_remains_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("coire_node.metrics.platform.system", lambda: "Darwin")

    def unavailable(path: str) -> None:
        raise OSError("native library unavailable")

    monkeypatch.setattr("coire_node.metrics.ctypes.CDLL", unavailable)
    assert m._read_process_thermal_level() is None


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (0, ThermalState.NOMINAL),
        (10, ThermalState.FAIR),
        (20, ThermalState.SERIOUS),
        (30, ThermalState.CRITICAL),
    ],
)
def test_thermal_state_maps_pressure_levels(
    monkeypatch: pytest.MonkeyPatch, level: int, expected: ThermalState
) -> None:
    class Out:
        stdout = f'"ThermalPressureLevel"={level}'.encode()

    monkeypatch.setattr("coire_node.metrics.shutil.which", lambda _: "/usr/sbin/ioreg")
    monkeypatch.setattr("coire_node.metrics.run_probe", lambda *a, **k: Out())
    assert m.read_thermal_state() is expected


def test_budget_flag_trips_when_the_agent_costs_too_much(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Studios are for inference; the agent must report when it overspends."""
    c = collector(budget_cpu_pct=0.0, budget_rss_bytes=1)
    assert c.sample().collection_budget_ok is False


def test_budget_flag_is_true_within_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    c = collector(budget_cpu_pct=100.0, budget_rss_bytes=10 * 1024**3)
    monkeypatch.setattr(c._proc, "cpu_percent", lambda interval=None: 25.0)
    assert c.sample().collection_budget_ok is True


def test_saturated_cpu_boundary_allows_sampling_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    c = collector(budget_cpu_pct=100.0, budget_rss_bytes=10 * 1024**3)
    monkeypatch.setattr(c._proc, "cpu_percent", lambda interval=None: 100.3)
    assert c.sample().collection_budget_ok is True


def test_latest_relabels_the_path_without_resampling() -> None:
    c = collector()
    first = c.sample()
    relabelled = c.latest(path=NodePath.FALLBACK)
    assert relabelled.path is NodePath.FALLBACK
    assert relabelled.sampled_at == first.sampled_at


def test_latest_samples_on_first_call() -> None:
    assert collector().latest().name == "coire-edge-a"
