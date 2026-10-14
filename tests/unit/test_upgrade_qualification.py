"""Historical reports survive upgrades but cannot authorize another runtime."""

from datetime import UTC, datetime, timedelta

import pytest

from coire_api.db import NodeRow
from coire_api.upgrades.qualification import runtime_matches
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "changed", "unknown", "stale", "future", "absent"])
def test_training_qualification_requires_current_observed_runtime(fault: str | None) -> None:
    now = datetime.now(UTC)
    nodes = [
        NodeRow(name=name, runtime_sha256="a" * 64, health_observed_at=now)
        for name in ("coire-edge-a", "coire-edge-b")
    ]
    if fault == "changed":
        nodes[1].runtime_sha256 = "b" * 64
    elif fault == "unknown":
        nodes[1].runtime_sha256 = None
    elif fault == "stale":
        nodes[1].health_observed_at = now - timedelta(seconds=31)
    elif fault == "future":
        nodes[1].health_observed_at = now + timedelta(seconds=1)
    elif fault == "absent":
        nodes = []
    assert runtime_matches(nodes, "a" * 64, Settings(upgrades_enabled=True), now=now) is (
        fault is None
    )
    assert runtime_matches(nodes, "a" * 64, Settings(upgrades_enabled=False), now=now)


@pytest.mark.parametrize(
    "fault", [None, "same_version_content", "swapped", "partial", "missing_runtime"]
)
def test_exact_pair_qualification_does_not_reuse_matching_version_strings(
    fault: str | None,
) -> None:
    from coire_api.db import LinkObservationRow
    from coire_api.link_probe_coordinator import versions_need_probe
    from coire_core.models import ProbeTransport

    signature = ("26.0", "26.0", "mlx-lm-0.31.3")
    original_pair = ("a" * 64, "b" * 64)
    now = datetime.now(UTC)
    reports = [
        LinkObservationRow(
            transport=transport,
            os_version_a=signature[0],
            os_version_b=signature[1],
            engine_version=signature[2],
            runtime_sha256_a=original_pair[0],
            runtime_sha256_b=None if fault == "missing_runtime" else original_pair[1],
            observed_at=now,
        )
        for transport in (ProbeTransport.JACCL, ProbeTransport.RING)
    ]
    if fault == "partial":
        reports.pop()
    current_pair = (
        ("c" * 64, original_pair[1])
        if fault == "same_version_content"
        else (original_pair[1], original_pair[0])
        if fault == "swapped"
        else original_pair
    )
    before = [(row.runtime_sha256_a, row.runtime_sha256_b, row.observed_at) for row in reports]
    assert versions_need_probe(reports, signature, current_pair) is (fault is not None)
    # Historical measurements remain inspectable and unchanged after invalidation.
    assert [
        (row.runtime_sha256_a, row.runtime_sha256_b, row.observed_at) for row in reports
    ] == before


@pytest.mark.parametrize("fault", [None, "same_version_content", "unknown", "stale", "future"])
def test_image_coexistence_qualification_uses_installed_content_and_fresh_observation(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_scheduler import image_admission

    settings = Settings(upgrades_enabled=True)
    monkeypatch.setattr(image_admission, "get_settings", lambda: settings)
    now = datetime.now(UTC)
    node = NodeRow(
        name="coire-edge-a", agent_version="0.2.0", runtime_sha256="a" * 64, health_observed_at=now
    )
    historical = image_admission.node_runtime_fingerprint(node)
    if fault == "same_version_content":
        node.runtime_sha256 = "b" * 64
    elif fault == "unknown":
        node.runtime_sha256 = None
    elif fault == "stale":
        node.health_observed_at = now - timedelta(seconds=settings.placement_health_freshness_s + 1)
    elif fault == "future":
        node.health_observed_at = now + timedelta(seconds=1)
    assert node.agent_version == "0.2.0"
    assert image_admission.current_image_runtime(node, now) is (
        fault in {None, "same_version_content"}
    )
    assert (image_admission.node_runtime_fingerprint(node) == historical) is (
        fault not in {"same_version_content", "unknown"}
    )
    # A retained report remains a historical value; qualification does not rewrite it.
    assert historical == image_admission.node_runtime_fingerprint(
        NodeRow(name="coire-edge-a", agent_version="0.2.0", runtime_sha256="a" * 64)
    )


@pytest.mark.parametrize("fault", [None, "changed", "unknown", "legacy", "stale"])
def test_harness_gate_requires_evaluated_native_content(fault: str | None) -> None:
    from coire_api.upgrades.qualification import harness_runtime_matches

    now = datetime.now(UTC)
    node = NodeRow(name="coire-edge-a", runtime_sha256="a" * 64, health_observed_at=now)
    evaluated: str | None = "a" * 64
    if fault == "changed":
        node.runtime_sha256 = "b" * 64
    elif fault == "unknown":
        node.runtime_sha256 = None
    elif fault == "legacy":
        evaluated = None
    elif fault == "stale":
        node.health_observed_at = now - timedelta(seconds=31)
    assert harness_runtime_matches(node, evaluated, Settings(upgrades_enabled=True), now=now) is (
        fault is None
    )
    assert harness_runtime_matches(node, evaluated, Settings(upgrades_enabled=False), now=now)


def test_evaluation_native_digest_is_distinct_and_legacy_payload_remains_stable() -> None:
    from pydantic import ValidationError

    from coire_core.models.evaluation import EvaluationRuntime

    historical = {
        "engine_version": "same-version",
        "harness_version": "same-version",
        "runtime_sha256": "b" * 64,
        "tokenizer_sha256": "c" * 64,
        "template_sha256": "d" * 64,
        "capability_sha256": "e" * 64,
    }
    legacy = EvaluationRuntime.model_validate(historical)
    assert legacy.model_dump(mode="json") == historical
    current = EvaluationRuntime.model_validate(historical | {"native_runtime_sha256": "a" * 64})
    assert current.native_runtime_sha256 != current.runtime_sha256
    assert EvaluationRuntime.model_validate_json(current.model_dump_json()) == current
    with pytest.raises(ValidationError):
        EvaluationRuntime.model_validate(historical | {"native_runtime_sha256": "version-string"})
