from __future__ import annotations

from itertools import pairwise

import pytest

from coire_api.instance.service import ALLOWED_TRANSITIONS
from coire_core.models.instance import InstanceState


def test_happy_path_is_exactly_the_declared_state_machine() -> None:
    path = [
        InstanceState.REQUESTED,
        InstanceState.RESERVING,
        InstanceState.LAUNCHING,
        InstanceState.WARMING,
        InstanceState.READY,
        InstanceState.DRAINING,
        InstanceState.STOPPED,
    ]
    assert all(target in ALLOWED_TRANSITIONS[source] for source, target in pairwise(path))


@pytest.mark.parametrize("state", list(InstanceState)[:-2])
def test_each_nonterminal_state_can_fail(state: InstanceState) -> None:
    assert InstanceState.FAILED in ALLOWED_TRANSITIONS[state]


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert ALLOWED_TRANSITIONS[InstanceState.STOPPED] == frozenset()
    assert ALLOWED_TRANSITIONS[InstanceState.FAILED] == frozenset()
