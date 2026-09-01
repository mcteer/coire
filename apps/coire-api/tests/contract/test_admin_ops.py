from datetime import UTC, datetime

from coire_api.app import create_app
from coire_api.console.ops import answer_from_snapshot
from coire_core.models.console import AskStatus, ConsoleCapabilities, ConsoleSnapshot
from coire_core.models.instance import ClusterState
from coire_core.settings import Settings


def test_ask_route_is_admin_guarded_strict_and_has_no_mutation_shape() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    operation = document["paths"]["/api/v1/admin/ops/ask"]["post"]
    assert operation["security"] == [{"HTTPBearer": []}]
    response = document["components"]["schemas"]["AskResponse"]
    assert response["additionalProperties"] is False
    assert not ({"tool", "action", "confirm_token"} & response["properties"].keys())


def test_read_only_answer_degrades_when_live_state_is_absent() -> None:
    observed_at = datetime(2026, 9, 1, tzinfo=UTC)
    snapshot = ConsoleSnapshot(
        observed_at=observed_at,
        cursor="1",
        capabilities=ConsoleCapabilities(),
        cluster=ClusterState(observed_at=observed_at, nodes=[], instances=[]),
        ledgers=[],
    )
    answer = answer_from_snapshot(snapshot)
    assert answer.status is AskStatus.UNAVAILABLE
    assert answer.sources == ["cluster"]
    assert "unavailable" in answer.answer
