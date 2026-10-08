"""Self-judging is forbidden across model identities and artifact aliases."""

import uuid

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.evaluation.authorization import preflight_evaluation_action, reject_self_judge
from coire_core.errors import EvaluationForbidden, EvaluationSelfJudge
from coire_core.models.adapters import InferenceTarget
from coire_core.models.auth import UserRole


def target(model: uuid.UUID | None = None, digest: str = "a") -> InferenceTarget:
    return InferenceTarget(
        model_id=model or uuid.uuid4(), variant_id=uuid.uuid4(), base_manifest_sha256=digest * 64
    )


@pytest.mark.parametrize("same_model,same_artifact", [(True, False), (False, True), (True, True)])
def test_self_judge_rejected_for_variants_adapters_and_artifact_aliases(
    same_model: bool, same_artifact: bool
) -> None:
    candidate = target()
    judge = target(candidate.model_id if same_model else None, "a" if same_artifact else "b")
    for subject in (
        candidate,
        candidate.model_copy(
            update={"adapter_id": uuid.uuid4(), "adapter_manifest_sha256": "c" * 64}
        ),
    ):
        with pytest.raises(EvaluationSelfJudge):
            reject_self_judge([subject], judge)


def test_distinct_registered_judge_allowed() -> None:
    reject_self_judge([target()], target(digest="b"))


@pytest.mark.parametrize(
    "kind",
    [PrincipalKind.SERVICE, PrincipalKind.OPS_SERVICE, PrincipalKind.RUN, PrincipalKind.ANONYMOUS],
)
def test_service_credentials_cannot_fabricate_human_evaluation_owner(kind: PrincipalKind) -> None:
    principal = Principal(
        kind=kind, user_id=uuid.uuid4(), role=UserRole.ADMIN, scopes=frozenset({"admin"})
    )
    with pytest.raises(EvaluationForbidden):
        preflight_evaluation_action(principal, method="GET", origin=None, browser_origin="")


def test_human_admin_mutations_require_browser_origin() -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4(), role=UserRole.ADMIN)
    with pytest.raises(EvaluationForbidden):
        preflight_evaluation_action(
            principal, method="POST", origin=None, browser_origin="https://coire.test"
        )
    assert (
        preflight_evaluation_action(
            principal,
            method="POST",
            origin="https://coire.test",
            browser_origin="https://coire.test",
        )
        == principal.user_id
    )


async def test_demoted_reader_refusal_has_durable_content_free_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from starlette.requests import Request

    import coire_api.evaluation.authorization as module
    from coire_core.models.audit import AuditOutcome
    from coire_core.settings import Settings

    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4(), role=UserRole.ADMIN)
    session = AsyncMock()

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncMock]:
        yield session

    live = AsyncMock(side_effect=EvaluationForbidden())
    audit = AsyncMock()
    monkeypatch.setattr(module, "session_scope", scope)
    monkeypatch.setattr(module, "authorize_evaluation_read", live)
    monkeypatch.setattr(module, "write_principal_audit", audit)
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = Settings()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/admin/evaluations",
            "headers": [],
            "app": app,
        }
    )
    with pytest.raises(EvaluationForbidden):
        await module.require_evaluation_reader(request, principal)
    audit.assert_awaited_once()
    assert audit.call_args.kwargs["outcome"] is AuditOutcome.REFUSED
    assert audit.call_args.kwargs["context"] == {"reason": "authorization"}
    session.commit.assert_awaited_once()
