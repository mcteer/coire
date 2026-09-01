from __future__ import annotations

from datetime import UTC, datetime

from coire_api.console.ops import degraded_action_refusal, is_action_request
from coire_core.models.console import ConsoleCapabilities, ConsoleSnapshot


def _snapshot() -> ConsoleSnapshot:
    now = datetime.now(UTC)
    return ConsoleSnapshot(
        observed_at=now,
        cursor="1",
        capabilities=ConsoleCapabilities(),
        cluster={"observed_at": now, "nodes": [], "instances": []},
        ledgers=[],
    )


def test_degraded_classifier_distinguishes_status_from_actions() -> None:
    assert not is_action_request("What is the cluster status?")
    assert not is_action_request("Which Studios are healthy?")
    assert is_action_request("Unload the idle instance")
    assert is_action_request("Please kill this run")
    assert is_action_request("PIN the admin model!")


def test_degraded_action_request_is_explicitly_read_only() -> None:
    response = degraded_action_refusal(_snapshot())
    assert response.status.value == "unavailable"
    assert "read-only degraded mode" in response.answer
    assert "cannot create an action proposal" in response.answer
