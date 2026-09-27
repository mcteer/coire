"""Feature 013 wire contracts at the public, scheduler, and node boundaries."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from coire_api.auth import Principal, PrincipalKind
from coire_api.mcp_calls import require_mcp_owner
from coire_api.openapi import OUTPUT, rendered
from coire_core.models.harness import HarnessRunRequest, ProfileName, TaskClass
from coire_core.models.mcp import (
    ApplyInput,
    ApplyResult,
    FileCitation,
    PlanResult,
    PlanStep,
    ResearchInput,
    ResearchResult,
    WorkspaceSource,
)
from coire_core.models.mcp import (
    TestStatus as McpTestStatus,
)
from coire_core.models.mcp import (
    TestSummary as McpTestSummary,
)
from coire_core.models.node import WorkspacePrepareRequest
from coire_core.models.registry import CapabilityProfile
from coire_core.models.runs import AgentRunCreate, RunContainerCreate


def _source() -> WorkspaceSource:
    return WorkspaceSource(repository_url="https://example.org/team/repo.git", revision="main")  # type: ignore[arg-type]


def test_repository_source_rejects_ambiguous_and_credentialed_urls() -> None:
    with pytest.raises(ValidationError):
        WorkspaceSource(revision="main")
    with pytest.raises(ValidationError):
        WorkspaceSource(
            repository_url="https://example.org/repo.git",  # type: ignore[arg-type]
            workspace_id=uuid.uuid4(),
            revision="main",
        )
    with pytest.raises(ValidationError):
        WorkspaceSource(
            repository_url="https://user:secret@example.org/repo.git",  # type: ignore[arg-type]
            revision="main",
        )
    with pytest.raises(ValidationError):
        WorkspaceSource(repository_url="http://example.org/repo.git", revision="main")  # type: ignore[arg-type]


def test_mcp_input_and_citation_are_bounded() -> None:
    request = ResearchInput(source=_source(), question="Where is the API?")
    assert request.model_dump(mode="json")["source"]["revision"] == "main"
    with pytest.raises(ValidationError):
        ResearchInput(source=_source(), question="x", extra_field=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ApplyInput(source=_source())
    with pytest.raises(ValidationError):
        ApplyInput(source=_source(), plan="go", plan_result_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        FileCitation(path="../secrets", line=1)
    with pytest.raises(ValidationError):
        FileCitation(path="/absolute", line=1)
    assert McpTestSummary(status=McpTestStatus.UNSUPPORTED).status is McpTestStatus.UNSUPPORTED


def test_node_prepare_cannot_change_harness_task_class() -> None:
    harness = HarnessRunRequest(
        profile=ProfileName.CODING,
        variant_id=uuid.uuid4(),
        task_class=TaskClass.READ,
        task="Research this repository",
        capability_profile=CapabilityProfile(),
        context_window=2048,
    )
    with pytest.raises(ValidationError):
        WorkspacePrepareRequest(
            run_id=uuid.uuid4(),
            source=_source(),
            task_class=TaskClass.WRITE,
            harness_request=harness,
        )


def test_existing_run_callers_keep_write_default() -> None:
    model_id = uuid.uuid4()
    request = AgentRunCreate(
        profile=ProfileName.CODING,
        primary_model_id=model_id,
        workspace_ref="existing",
        permitted_model_ids=frozenset({model_id}),
    )
    assert request.task_class is TaskClass.WRITE
    container = RunContainerCreate(
        run_id=uuid.uuid4(),
        profile=ProfileName.CODING,
        model_id=model_id,
        variant_id=uuid.uuid4(),
        image="localhost/agent@sha256:" + "a" * 64,
        argv=["-m", "coire_agent"],
        workspace_ref="existing",
        run_token="x" * 32,
        gateway_url="http://coire-gateway:8080/v1",
        limits=request.limits,
    )
    assert container.task_class is TaskClass.WRITE


def test_run_contract_openapi_is_regenerated() -> None:
    assert OUTPUT.read_text() == rendered()


def test_tool_results_require_citations_steps_and_bounded_diff() -> None:
    run_id = uuid.uuid4()
    revision = "a" * 40
    with pytest.raises(ValidationError):
        ResearchResult(
            result_id=uuid.uuid4(),
            run_id=run_id,
            source_revision=revision,
            answer="unsubstantiated",
            citations=[],
        )
    with pytest.raises(ValidationError):
        PlanResult(
            result_id=uuid.uuid4(),
            run_id=run_id,
            source_revision=revision,
            goal="change behavior",
            steps=[PlanStep(description="change it", acceptance_criteria=[])],
        )
    with pytest.raises(ValidationError):
        ApplyResult(
            run_id=run_id,
            branch="main",
            base_revision=revision,
            head_revision=revision,
            diff_excerpt="",
            diff_truncated=False,
            tests=McpTestSummary(status=McpTestStatus.NOT_FOUND),
            artifact_id=uuid.uuid4(),
        )
    with pytest.raises(ValidationError):
        ApplyResult(
            run_id=run_id,
            branch="coire/change",
            base_revision=revision,
            head_revision=revision,
            diff_excerpt="x" * (1_048_576 + 1),
            diff_truncated=False,
            tests=McpTestSummary(status=McpTestStatus.FAILED),
            artifact_id=uuid.uuid4(),
        )


def test_mcp_call_requires_user_bound_scoped_key() -> None:
    owner_id = uuid.uuid4()
    assert (
        require_mcp_owner(
            Principal(
                kind=PrincipalKind.API_KEY,
                scopes=frozenset({"mcp"}),
                user_id=owner_id,
            )
        )
        == owner_id
    )
    for principal in (
        Principal(kind=PrincipalKind.API_KEY, user_id=owner_id),
        Principal(kind=PrincipalKind.API_KEY, scopes=frozenset({"mcp"})),
        Principal(kind=PrincipalKind.ADMIN, scopes=frozenset({"mcp"}), user_id=owner_id),
    ):
        with pytest.raises(PermissionError):
            require_mcp_owner(principal)
