"""Deterministic, read-only status answers used until the model-backed ops harness ships."""

from __future__ import annotations

from coire_core.models.console import AskResponse, AskStatus, ConsoleSnapshot

_ACTION_TERMS = frozenset(
    {
        "kill",
        "load",
        "pin",
        "restart",
        "stop",
        "terminate",
        "unload",
        "unpin",
    }
)


def is_action_request(question: str) -> bool:
    """Conservatively identify mutation-like requests without inference."""

    words = {word.strip(".,!?;:()[]{}").casefold() for word in question.split()}
    return bool(words & _ACTION_TERMS)


def degraded_action_refusal(snapshot: ConsoleSnapshot) -> AskResponse:
    return AskResponse(
        status=AskStatus.UNAVAILABLE,
        answer=(
            "Coire is in read-only degraded mode because the pinned admin model is unavailable. "
            "It cannot create an action proposal until model service recovers."
        ),
        observed_at=snapshot.observed_at,
        sources=["cluster"],
    )


def answer_from_snapshot(snapshot: ConsoleSnapshot) -> AskResponse:
    unhealthy = [
        node.name for node in snapshot.cluster.nodes if node.reachability.value != "healthy"
    ]
    if not snapshot.cluster.nodes:
        return AskResponse(
            status=AskStatus.UNAVAILABLE,
            answer="Live node state is unavailable, so Coire cannot ground an answer safely.",
            observed_at=snapshot.observed_at,
            sources=["cluster"],
        )
    detail = (
        "All registered Studios are healthy."
        if not unhealthy
        else f"Attention is needed on: {', '.join(unhealthy)}."
    )
    return AskResponse(
        status=AskStatus.ANSWERED,
        answer=(
            f"{detail} There are {len(snapshot.cluster.instances)} model instances and "
            f"{len(snapshot.alerts)} active alerts."
        ),
        observed_at=snapshot.observed_at,
        sources=["cluster.nodes", "cluster.instances", "alerts"],
    )
