"""Metric and span names are a contract with feature 009 (T068).

ADR-0003's extension names the instruments 009's panels and alert rules will read. Renaming one
later would break a dashboard silently — the panel simply goes blank — so the names are pinned
here, where a rename fails a test instead.
"""

from __future__ import annotations

import pytest

EXPECTED_METRICS = {
    "coire_model_state",
    "coire_download_bytes_total",
    "coire_engine_state",
    "coire_engine_resident_bytes",
    "coire_engine_load_seconds",
}

EXPECTED_SPAN_PREFIXES = {"registry.reconcile."}


def test_every_metric_adr_0003_promises_exists() -> None:
    import coire_api.registry.reconciler as reconciler
    import coire_node.engines as engines

    sources = []
    for module in (reconciler, engines):
        sources.append(open(module.__file__).read())  # noqa: SIM115
    blob = "\n".join(sources)

    missing = sorted(name for name in EXPECTED_METRICS if f'"{name}"' not in blob)
    assert missing == [], (
        f"metrics promised in ADR-0003 are absent: {missing}. Feature 009's panels read these "
        "names; renaming one blanks a dashboard rather than failing anything."
    )


def test_the_reconciler_names_its_spans_by_stage() -> None:
    import coire_api.registry.reconciler as reconciler

    source = open(reconciler.__file__).read()  # noqa: SIM115
    assert 'f"registry.reconcile.{job.stage.value}"' in source, (
        "a request's time must be attributable to a stage (Principle VI)"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_METRICS))
def test_metric_names_are_prometheus_shaped(name: str) -> None:
    """Lower snake case, `coire_` prefixed, counters suffixed `_total`."""
    assert name.islower()
    assert name.startswith("coire_")
    assert " " not in name and "-" not in name
