"""Suite versions freeze configuration and judge identity, independent of retirement."""

from datetime import UTC, datetime

from coire_api.evaluation.catalog import build_suite, definition_digest
from coire_core.models.evaluation import EvaluationSuiteRegistration


def test_catalog_definition_digest_excludes_read_time_and_attribution() -> None:
    request = EvaluationSuiteRegistration(
        suite_id="task-local", version=1, template_id="task-coding-instructions"
    )
    suite = build_suite(request, owner=None, judge=None, now=datetime.now(UTC))
    assert suite.content_sha256 == definition_digest(suite)
    assert (
        definition_digest(suite.model_copy(update={"retired": True, "registry_version": 2}))
        == suite.content_sha256
    )
    changed = suite.model_copy(
        update={"generation": suite.generation.model_copy(update={"seed": 42})}
    )
    assert definition_digest(changed) != suite.content_sha256
