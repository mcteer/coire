# Feature Specification: Agent Harness and Capability Profiles

**Feature Branch**: `010-agent-harness-profiles`

**Roadmap ID**: 008 (Phase 3 — Agents)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Single `coire-agent` image; Pydantic AI agents `coding`, `general`, `image`, `ops`; capability-profile-driven tool-calling/structured-output strategies; context budgeting; harness evaluation suite (`coire eval harness`) that marks models `verified`."

## Overview

Frontier-API harnesses assume reliable native tool calling, long contexts, and strong instruction following. Open-weights models at 4-bit on local hardware are less forgiving, and engine tool support depends on each model's chat template. This feature builds a harness that treats model capability as data rather than assumption: one agent image with four profiles, strategies selected from the registry's capability profile, disciplined context budgeting, and an evaluation suite that measures whether a model can actually do the job before the router will trust it with one.

## Clarifications

### Session 2026-08-29

- Q: How does a model with no native tool calling still work? → A: The harness falls back to a delimited-JSON tool protocol it parses itself, with validation failures fed back to the model as retries. The capability profile selects the strategy, so the same agent code drives a native-tool-calling model and a text-only one without branching in the agent logic.
- Q: What does `verified` gate, exactly? → A: Write-capable tasks. The router refuses to route an `apply`-class task to a model whose profile is not verified. Read-only work — research, planning, chat — is allowed on unverified models, because the cost of a bad answer is a wasted response, while the cost of a bad edit is a damaged repository.
- Q: Where does the ops profile live? → A: In a separate image from the user-facing profiles. The user-facing image must not contain the admin API client at all, so that a compromised user run has no admin surface to reach even in principle. Both images are built from the same harness codebase.
- Q: How is context budgeted? → A: The harness measures prompt tokens from reported usage, keeps a rolling summary of older turns produced by the pinned admin model, truncates tool outputs with head-and-tail byte caps, and pins the system prompt and current task at the front. Earlier turns are never rewritten, only appended to, because the engine's prompt cache rewards a stable prefix.
- Q: How many tools may a profile expose? → A: A small flat set — roughly eight to ten with short descriptions and no nested unions in argument schemas — with anything further loaded on demand through a meta-tool. Open-weights coders degrade quickly past that, and deeply-nested schemas are mishandled by prompted-JSON models specifically.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - One harness drives models of differing capability (Priority: P1)

The same agent profile runs successfully against a model with native tool calling and against one without, with the difference absorbed by the capability profile rather than by the agent's logic.

**Why this priority**: This is the feature's central claim. Without it every new model is a code change, which contradicts Principle V.

**Independent Test**: Run one profile's task against two models with different tool-calling capabilities and confirm both complete.

**Acceptance Scenarios**:

1. **Given** a model whose profile declares native tool calling, **When** an agent runs, **Then** tools are offered as schemas and calls are parsed natively.
2. **Given** a model whose profile declares no tool calling, **When** the same agent runs, **Then** the harness uses its own delimited-JSON protocol and the task still completes.
3. **Given** a model producing a malformed tool call, **When** parsing fails, **Then** the harness retries with the validation error rather than failing the run.
4. **Given** repeated failures beyond the retry ceiling, **When** the ceiling is reached, **Then** the run fails with a diagnosis naming the parsing failure.
5. **Given** a model declaring thinking-tag reasoning, **When** it responds, **Then** reasoning blocks are excluded from tool-call parsing and surfaced separately.

---

### User Story 2 - Structured output is validated, never trusted (Priority: P1)

An agent asked for structured output validates what it gets, retries usefully on failure, and has a last-resort repair path rather than propagating malformed data.

**Why this priority**: Every downstream consumer of an agent result assumes shape. Malformed structured output that escapes the harness becomes a failure somewhere far less diagnosable.

**Independent Test**: Force a model to emit invalid structured output and confirm the harness recovers or fails cleanly.

**Acceptance Scenarios**:

1. **Given** a model claiming native structured output, **When** it is used, **Then** that mode is used and the result is still validated.
2. **Given** invalid output, **When** validation fails, **Then** the harness retries with the validation error and a reduced schema.
3. **Given** repeated validation failure, **When** the ceiling is reached, **Then** a repair attempt is made against the pinned admin model before the run fails.
4. **Given** any structured result, **When** it is returned, **Then** it has been validated against its declared type.

---

### User Story 3 - Long tasks stay within context (Priority: P2)

An agent working a long task keeps the system prompt and current task in view, summarises older turns, and truncates oversized tool outputs rather than overflowing.

**Why this priority**: Local models have smaller usable contexts than frontier APIs, and overflow presents as sudden incoherence. It matters greatly but the harness is demonstrable on short tasks first.

**Independent Test**: Run a task long enough to exceed the model's context and confirm it completes coherently.

**Acceptance Scenarios**:

1. **Given** a conversation approaching the context limit, **When** the harness prepares the next call, **Then** older turns are replaced by a rolling summary while the system prompt and current task remain.
2. **Given** a tool returning a very large output, **When** it is incorporated, **Then** it is truncated head-and-tail within a byte cap and the truncation is visible.
3. **Given** a rolling summary being produced, **When** it is generated, **Then** it uses the pinned admin model rather than spending the task model's context.
4. **Given** any turn, **When** it is appended, **Then** earlier turns are not rewritten.

---

### User Story 4 - A model earns the right to write (Priority: P1)

An evaluation suite scores a model on tool calling, structured output, edit application, and long context; the score is visible; and the router refuses write-capable tasks to models that have not passed.

**Why this priority**: This is the roadmap's named acceptance bar and the safety property that makes the MCP `apply` tool tolerable at all.

**Independent Test**: Run the suite against several models, confirm a scorecard, and confirm an unverified model is refused an `apply` task.

**Acceptance Scenarios**:

1. **Given** a model in the registry, **When** the harness evaluation suite is run against it, **Then** a scorecard covering tool calling, structured output, edit application, and long context is produced and stored.
2. **Given** a model that passes, **When** the suite completes, **Then** its profile is marked verified with the score and date recorded.
3. **Given** an unverified model, **When** a write-capable task is routed, **Then** it is refused with a reason naming verification.
4. **Given** an unverified model, **When** a read-only task is routed, **Then** it proceeds normally.
5. **Given** a re-run after a model or engine change, **When** it completes, **Then** the new score is stored as a new record and regression is visible.

---

### Edge Cases

- A model's chat template is broken or missing: the registry MUST be able to supply an override template that is versioned in the repository.
- A model leaks role markers into output: the profile MUST be able to declare explicit stop sequences.
- A reasoning model spends its budget thinking: thinking MUST be capped per run, since reasoning tokens are wall-clock on local hardware.
- A tool schema contains nested unions: the harness MUST avoid emitting such schemas to prompted-JSON models.
- A profile needs a specialised tool: it MUST be loadable on demand rather than permanently enlarging the toolset.
- The pinned admin model is unavailable when a summary or repair is needed: the harness MUST degrade — truncating rather than summarising, failing validation rather than repairing — instead of stalling.
- An evaluation suite is run against a model that cannot load: it MUST fail as an infrastructure error, not as a low score.
- A model is verified and later republished with a different variant: verification MUST attach to the variant, not the base name.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide one user-facing agent image with `coding`, `general`, and `image` profiles selected at run time.
- **FR-002**: The `ops` profile MUST ship in a separate image containing the admin client, and the user-facing image MUST NOT contain that client.
- **FR-003**: Each profile MUST have its own system prompt, output type, and toolset.
- **FR-004**: Tool-calling strategy MUST be selected from the model's capability profile, supporting native, JSON-mode, and a harness-parsed delimited protocol.
- **FR-005**: Structured-output strategy MUST be selected from the capability profile, and all structured results MUST be validated regardless of strategy.
- **FR-006**: Validation failures MUST be retried with the error and a reduced schema, and MUST fall back to a repair attempt on the pinned admin model before failing.
- **FR-007**: The harness MUST strip reasoning blocks from tool-call parsing and surface them separately, when the profile declares them.
- **FR-008**: Thinking budgets MUST be capped per run.
- **FR-009**: Profiles MUST set sampling parameters and explicit stop sequences per task class.
- **FR-010**: Each profile MUST expose a small flat toolset with short descriptions, avoiding nested unions in argument schemas.
- **FR-011**: Additional tools MUST be loadable on demand through a meta-tool rather than always present.
- **FR-012**: The harness MUST budget context by measuring reported prompt tokens, summarising older turns using the pinned admin model, truncating tool outputs within byte caps, and pinning the system prompt and current task.
- **FR-013**: The harness MUST only append to conversation history and MUST NOT rewrite earlier turns.
- **FR-014**: The registry MUST support an override chat template per model, versioned in the repository.
- **FR-015**: The system MUST provide an evaluation suite covering tool calling, structured output, edit application, and long context.
- **FR-016**: Suite results MUST be stored per model variant with a score and date, and re-runs MUST be stored as new records.
- **FR-017**: A model variant MUST be marked verified only by passing the suite.
- **FR-018**: The router MUST refuse write-capable tasks to unverified model variants, and MUST allow read-only tasks.
- **FR-019**: Agents MUST reach models only through the gateway, never directly.
- **FR-020**: The harness MUST degrade gracefully when the pinned admin model is unavailable rather than stalling.

### Key Entities

- **Agent Profile**: A configured agent. Name, system prompt, output type, toolset, sampling parameters, stop sequences, model preference by tag.
- **Capability Profile**: Declared model behaviour used to select strategies. Tool-calling mode, structured-output mode, context window, reasoning style, parallel-tool support, override template reference, verified flag.
- **Harness Evaluation**: One suite run. Model variant, per-category scores, overall verdict, engine and harness versions, run-at.
- **Context Budget**: Per-run accounting. Token limit, current usage, summary state, truncation events.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The evaluation suite runs against at least three open-weights models and produces a scorecard for each.
- **SC-002**: Write-capable tasks are refused for unverified models in 100% of attempts; read-only tasks are permitted.
- **SC-003**: One profile completes the same task against a native-tool-calling model and a text-only model, with no agent-code branching.
- **SC-004**: Structured results are validated in 100% of runs; no unvalidated structured output escapes the harness.
- **SC-005**: A task whose history exceeds the model's context completes coherently through summarisation.
- **SC-006**: The user-facing agent image contains no admin client, verified by image inspection.
- **SC-007**: Malformed tool calls are recovered by retry in the majority of induced cases, and failures beyond the ceiling are diagnosed specifically.
- **SC-008**: Verification is recorded against a model variant, and republishing a different variant does not inherit it.

## Assumptions

- Features 001–009 have shipped: models carry capability profiles, the gateway serves them, and telemetry exists to observe runs.
- Agents run as containers on the Studios; that orchestration is feature 011. This feature builds the harness and can be exercised directly during development before run brokering exists.
- The pinned admin model on Studio B is resident and is used for summarisation, repair, and cheap utility calls.
- Agent profiles reference models by tag and preference order rather than by name, so publishing a new model changes agent behaviour without a code change.
- The evaluation suite is runnable on a Studio in minutes; it is not a research benchmark.
- Task and judge evaluation suites are feature 017; this feature provides only the harness capability suite.
- The MCP tools that consume these profiles are feature 013; the chat UI's code mode is feature 014.
- Per Principle VII, integration tests run against a model of 1 GB or less; the multi-model scorecard is produced manually on the real cluster.
