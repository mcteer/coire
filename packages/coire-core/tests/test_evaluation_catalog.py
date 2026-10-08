"""Installed fixtures have stable version/content identities and no executable plugins."""

import hashlib
import json

from coire_core.evaluation_suites import cases, templates
from coire_core.models.evaluation import EvaluationCase


def test_installed_catalog_case_digests_bounds_and_copy_isolation() -> None:
    for item in templates():
        fixtures = cases(item.template_id)
        assert len(fixtures) == item.case_count <= 32
        assert len({case.id for case in fixtures}) == len(fixtures)
        assert "third-party" in item.license
        wire = json.dumps(
            [case.model_dump(mode="json") for case in fixtures],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        assert hashlib.sha256(wire.encode()).hexdigest() == item.cases_sha256
        assert all(EvaluationCase.model_validate(case.model_dump()) == case for case in fixtures)
        fixtures[0].prompt = "tampered"
        assert cases(item.template_id)[0].prompt != "tampered"


def test_harness_preserves_all_four_categories_and_task_has_both_capabilities() -> None:
    assert {case.category for case in cases("harness-capability")} == {
        "tool_calling",
        "structured_output",
        "edit_application",
        "long_context",
    }
    assert {case.category for case in cases("task-coding-instructions")} == {
        "coding",
        "instruction",
    }
