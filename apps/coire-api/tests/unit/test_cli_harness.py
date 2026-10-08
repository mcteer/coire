"""Core CLI has no suite runner; fixed capability scoring belongs to Studio workers."""

from coire_api import cli
from coire_core.evaluation_suites import cases


def test_harness_cli_has_no_local_suite_execution_path() -> None:
    assert not hasattr(cli, "_run_suite")
    fixtures = cases("harness-capability")
    assert {case.category for case in fixtures} == {
        "tool_calling",
        "structured_output",
        "edit_application",
        "long_context",
    }
    # Actual case generation and strict scoring are tested in the Studio agent suite.
    assert len(fixtures) == 4
