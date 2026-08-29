# Feature Specification: MCP Server — Research, Plan, Apply

**Feature Branch**: `013-mcp-server`

**Roadmap ID**: 011 (Phase 3 — Agents)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Streamable-HTTP MCP endpoint with exactly three coding tools backed by agent runs on cloned workspaces; `apply` produces a branch + diff + test summary."

## Overview

This feature exposes Coire's coding capability to the editors and agent tools people already use, through a deliberately narrow interface: exactly three tools that mirror a spec-driven loop — research, plan, apply. Each call becomes a sandboxed agent run on a cloned workspace, inheriting every containment property feature 011 established. The narrowness is the design: no general chat, no image tools, and no administrative surface is reachable over this protocol.

## Clarifications

### Session 2026-08-29

- Q: Why exactly three tools? → A: Because the loop they describe is the one that produces reviewable changes, and because every additional tool is additional attack surface on a public, authenticated-but-remote endpoint. Read-only understanding, read-only planning, and one write operation that always lands on a branch is a complete loop without granting anything broader.
- Q: What does `apply` do with its changes? → A: It works on a fresh clone, commits to a new branch, runs whatever tests it finds, and returns the branch name, the diff, and the test summary. It never pushes to a default branch. A human reviews and merges; the platform never completes that step.
- Q: Which models may serve each tool? → A: Research and plan may use any published model, including unverified ones, since they only read. Apply requires a verified model, enforced by the router from feature 010. This is the difference between a wasted answer and a damaged repository.
- Q: How does a caller authenticate? → A: With the same Coire API keys as the rest of the platform, scoped to include MCP. There is no separate credential system for this endpoint, and a key without the MCP scope is refused.
- Q: Where does the workspace come from? → A: Either a repository URL the platform clones on the caller's behalf, or a previously registered workspace. The run container itself never reaches the network, so the clone is prepared by the platform and mounted, consistent with feature 011.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A developer completes a research to plan to apply loop from their editor (Priority: P1)

A developer adds Coire as an MCP server in their tool, asks a question about a repository, gets a plan, applies it, and receives a branch with a diff and test results.

**Why this priority**: This is the roadmap's named acceptance bar and the entire purpose of the feature.

**Independent Test**: Configure a client against the endpoint and complete all three tools in sequence on a sample repository.

**Acceptance Scenarios**:

1. **Given** a configured MCP client with a valid scoped key, **When** it connects, **Then** exactly three tools are advertised.
2. **Given** a repository and a question, **When** research runs, **Then** findings are returned with file and line citations and no file is modified.
3. **Given** a goal and optional prior research, **When** plan runs, **Then** a step-by-step change plan with acceptance criteria is returned and no file is modified.
4. **Given** a plan, **When** apply runs, **Then** a new branch is created, changes are committed, discovered tests are run, and the branch name, diff, and test summary are returned.
5. **Given** any apply, **When** it completes, **Then** nothing has been pushed to the repository's default branch.

---

### User Story 2 - The surface stays narrow (Priority: P1)

Nothing beyond the three coding tools is reachable over this protocol, regardless of what a caller asks for.

**Why this priority**: This endpoint is authenticated but remote and speaks to model-driven clients. Its safety comes primarily from having very little to reach.

**Independent Test**: Enumerate the advertised tools and attempt administrative and chat operations, confirming none exist.

**Acceptance Scenarios**:

1. **Given** a connected client, **When** it enumerates tools, **Then** only research, plan, and apply are present.
2. **Given** a client attempting a general chat, image, or administrative operation, **When** it does so, **Then** no such capability exists to invoke.
3. **Given** a key without the MCP scope, **When** it connects, **Then** it is refused.
4. **Given** an unauthenticated connection, **When** it is attempted, **Then** it is refused.

---

### User Story 3 - Every call is a sandboxed, killable run (Priority: P1)

Each tool call executes as an agent run with the same containment, limits, timeout, and kill switch as any other run.

**Why this priority**: This is what makes remotely-triggered code execution acceptable. Reusing feature 011 rather than building a second execution path is the point.

**Independent Test**: Start a long-running apply, kill it from the admin console, and confirm it stops and its credential is invalidated.

**Acceptance Scenarios**:

1. **Given** any tool call, **When** it executes, **Then** it runs as an agent run on a Studio with one network route, resource limits, and a wall-clock timeout.
2. **Given** a running tool call, **When** an admin kills it, **Then** it stops and its run token is invalidated.
3. **Given** a tool call exceeding its timeout, **When** the timeout passes, **Then** it is terminated and the caller receives a timeout result rather than hanging.
4. **Given** any tool call, **When** it is inspected, **Then** it appears in the platform's run listing with its owner and duration.

---

### User Story 4 - Apply refuses unverified models (Priority: P2)

A write operation is refused when the model backing it has not passed the harness evaluation.

**Why this priority**: The verification gate exists precisely for this operation. It ranks second only because apply is unusable before the loop works at all.

**Independent Test**: Configure apply against an unverified model and confirm refusal with a reason.

**Acceptance Scenarios**:

1. **Given** an unverified model, **When** apply is invoked, **Then** it is refused with a reason naming verification.
2. **Given** the same unverified model, **When** research or plan is invoked, **Then** it proceeds normally.
3. **Given** a model that later passes verification, **When** apply is invoked, **Then** it proceeds without configuration changes.

---

### Edge Cases

- The repository cannot be cloned: the tool MUST return a clear error naming the clone failure rather than an empty result.
- Tests are absent from the repository: apply MUST report that no tests were found rather than claiming success or failure.
- Tests fail after the change: apply MUST still return the branch and diff with a failing test summary, since a failing result the developer can inspect is more useful than a discarded one.
- The diff is very large: it MUST be truncated with the truncation stated, and the branch MUST remain complete.
- Two applies run against one workspace concurrently: each MUST work on its own clone and produce its own branch.
- A plan identity from an earlier session is referenced: it MUST be resolvable if retained and MUST fail clearly if expired.
- The connection drops mid-call: the run MUST be terminated rather than continuing without a listener.
- A caller requests a model they are not entitled to: it MUST be refused exactly as the gateway refuses it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST expose an MCP endpoint over streamable HTTP advertising exactly three tools: research, plan, and apply.
- **FR-002**: No general chat, image, or administrative capability may be reachable over this endpoint.
- **FR-003**: The endpoint MUST authenticate with platform API keys and MUST require an MCP scope.
- **FR-004**: Research MUST perform a read-only agent run and return findings with file and line citations.
- **FR-005**: Plan MUST perform a read-only agent run and return a step-by-step change plan with acceptance criteria, optionally consuming a prior research result.
- **FR-006**: Apply MUST run on a fresh clone, create a new branch, commit changes, run discovered tests, and return branch name, diff, and test summary.
- **FR-007**: Apply MUST NOT push to a repository's default branch under any circumstance.
- **FR-008**: Apply MUST be refused when the backing model variant is not verified; research and plan MUST be permitted on unverified models.
- **FR-009**: Every tool call MUST execute as an agent run inheriting sandbox confinement, resource limits, wall-clock timeout, and the kill switch.
- **FR-010**: Run containers MUST NOT reach the network; workspaces MUST be prepared by the platform and mounted.
- **FR-011**: Every tool call MUST appear in the platform's run listing with owner, tool, duration, and outcome.
- **FR-012**: A dropped client connection MUST terminate the underlying run.
- **FR-013**: Apply MUST report absent tests distinctly from passing or failing tests.
- **FR-014**: Apply MUST return its branch and diff even when tests fail.
- **FR-015**: Oversized diffs MUST be truncated with the truncation stated, without truncating the branch contents.
- **FR-016**: Concurrent applies against one workspace MUST use separate clones and produce separate branches.
- **FR-017**: A caller MUST be refused any model they are not entitled to, consistently with the gateway.
- **FR-018**: Every tool call MUST be attributable to its caller for usage and audit purposes.
- **FR-019**: The MCP service MUST run as its own container, independently restartable without dropping chat traffic.
- **FR-020**: Clone failures and other infrastructure errors MUST be reported distinctly from agent failures.

### Key Entities

- **MCP Tool Call**: One invocation. Tool, caller, credential, workspace reference, parameters, backing run, outcome, timings.
- **Workspace**: The code a call operates on. Source repository or registered workspace, clone location, prepared-at, lifetime.
- **Research Result**: Read-only findings. Question, findings with file and line citations, model used, run reference.
- **Plan Result**: A proposed change. Goal, ordered steps, acceptance criteria, optional research reference, model used, run reference.
- **Apply Result**: A produced change. Branch name, diff, test summary with counts and outcome, model used, run reference.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A standard MCP client adds Coire as a server and completes a research, plan, and apply loop against a sample repository.
- **SC-002**: Exactly three tools are advertised; no administrative, chat, or image capability is reachable, verified by enumeration.
- **SC-003**: Apply never pushes to a default branch, across all trials.
- **SC-004**: Apply against an unverified model is refused in 100% of attempts, while research and plan succeed.
- **SC-005**: An in-flight tool call is killable from the admin console and stops within 5 seconds.
- **SC-006**: A key lacking the MCP scope is refused in 100% of attempts.
- **SC-007**: Apply with failing tests still returns a usable branch, diff, and failing summary.
- **SC-008**: Restarting the MCP service does not interrupt chat traffic.

## Assumptions

- Features 001–011 have shipped: the gateway, credentials and scopes, the agent harness with its coding profile and verification gate, and sandboxed run orchestration.
- The MCP service runs as its own container on core, sharing the codebase with the API but built and deployed as a separate image, per Principle II-a.
- The coding profile's toolset, context budgeting, and model preferences come from feature 010 and are not redefined here.
- Workspace preparation, including cloning, happens outside the run container because runs have no network egress.
- Research and plan results may be retained so a later call can reference them; retention duration is a configuration concern.
- The chat UI's code mode uses the same coding profile but is feature 014 and is not exposed through this endpoint.
- Per Principle VII, integration tests exercise the loop against a tiny model on a sample repository.
