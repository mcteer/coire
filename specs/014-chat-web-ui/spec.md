# Feature Specification: Chat Web UI

**Feature Branch**: `014-chat-web-ui`

**Roadmap ID**: 012 (Phase 4 — Chat UI, images, training)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Claude-style conversation UI: streaming, task-grouped model picker showing only published models with descriptions, tags, and load state (with warm-up estimate for cold models), file upload, code mode using the coding agent, conversation history, thinking-block display."

## Overview

This is the platform's face for people who are not going to use an SDK. It is a conversation interface with streaming responses, a model picker that groups by task and tells the truth about whether a model is warm, file upload, a code mode backed by the coding agent, persistent history, and proper display of reasoning blocks. Its acceptance bar is explicitly non-technical: someone who does not know what a placement policy is should be able to chat, switch models, and understand why a response is slow to start.

## Clarifications

### Session 2026-08-29

- Q: What does the picker show, and what does it hide? → A: Only published, `ready` models the user is entitled to, grouped by task tag, each with a display name, a one-line description, tags, context size, size class, and live load state. Cold models show an estimated warm-up time. Everything about placement, variants, nodes, and internal identifiers stays out of the user's view.
- Q: How is a cold model's delay presented? → A: As an expected wait with the estimate shown before the user commits, and as visible progress once they do. The gateway already holds the connection and streams keep-alives; the interface's job is to make the wait legible rather than looking hung.
- Q: What is code mode? → A: The same conversation surface backed by the coding agent profile rather than a direct model call, so it runs as a sandboxed agent run with tools. It is presented as a mode of the conversation, not a separate application.
- Q: Are uploaded files retrievable later? → A: Yes, they are attached to the conversation and re-usable within it. Retrieval over file contents for the general agent needs an embedding model, which is explicitly backlog; this feature provides upload, attachment, and passing content to the model within context limits.
- Q: How are reasoning blocks handled? → A: Displayed separately from the answer and collapsed by default, never silently discarded and never mixed into the answer text. Models that emit reasoning declare it in their capability profile, so the interface knows what to expect.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A non-technical user chats and switches models (Priority: P1)

Someone with no knowledge of the platform's internals opens the UI, picks a model, has a streaming conversation, and switches models mid-session.

**Why this priority**: This is the roadmap's acceptance bar stated almost verbatim, and the reason the feature exists.

**Independent Test**: Have someone unfamiliar with the platform complete a conversation and a model switch without assistance.

**Acceptance Scenarios**:

1. **Given** an authenticated user, **When** they open the UI, **Then** they see a conversation surface and a model picker grouped by task.
2. **Given** a selected model, **When** the user sends a message, **Then** the response streams incrementally.
3. **Given** an ongoing conversation, **When** the user switches models, **Then** the conversation continues with the new model and the switch is visible in the transcript.
4. **Given** the picker, **When** it is opened, **Then** each model shows a display name, one-line description, tags, context size, and load state.
5. **Given** any model the user is not entitled to, **When** the picker is opened, **Then** it is absent.

---

### User Story 2 - Waiting for a cold model is legible (Priority: P1)

A user selects a model that is not loaded and understands that it is warming up and roughly how long it will take, rather than facing an apparently frozen interface.

**Why this priority**: This is the single most common confusing moment on a platform where a large model takes minutes to load, and the roadmap names it explicitly.

**Independent Test**: Select a cold model, send a message, and confirm the interface communicates warm-up and then streams.

**Acceptance Scenarios**:

1. **Given** a cold model in the picker, **When** it is displayed, **Then** its cold state and an estimated warm-up time are shown before selection.
2. **Given** a message sent to a cold model, **When** the load begins, **Then** the interface shows warm-up progress rather than an idle spinner.
3. **Given** the load completing, **When** it does, **Then** the response begins streaming without user action.
4. **Given** a load that fails, **When** it fails, **Then** the user sees a clear failure with a suggested next step rather than a silent stall.

---

### User Story 3 - Conversations persist and can be revisited (Priority: P2)

A user returns later and finds their conversations, with which model produced which response.

**Why this priority**: Without history the interface is a toy. It ranks below the core loop only because the loop must work first.

**Independent Test**: Hold a conversation, reload, and confirm it is intact with model attribution.

**Acceptance Scenarios**:

1. **Given** past conversations, **When** the user opens the UI, **Then** they are listed most recent first and can be reopened.
2. **Given** a reopened conversation, **When** it is displayed, **Then** every message appears with the model that produced it.
3. **Given** a conversation, **When** the user deletes it, **Then** it is removed from their view.
4. **Given** another user's conversation, **When** access is attempted, **Then** it is refused.

---

### User Story 4 - Code mode does real work safely (Priority: P2)

A user switches to code mode and the coding agent works with tools inside a sandbox, with its activity visible.

**Why this priority**: It is a major capability, but the platform is valuable with plain chat alone, and code mode depends on run orchestration.

**Independent Test**: Use code mode on a workspace and confirm it runs as a sandboxed agent run with visible tool activity.

**Acceptance Scenarios**:

1. **Given** code mode, **When** a request is made, **Then** it executes as a sandboxed agent run rather than a direct model call.
2. **Given** a running code-mode request, **When** the agent uses tools, **Then** tool activity is visible as it happens.
3. **Given** a code-mode run, **When** the user stops it, **Then** the run is killed and its credential invalidated.
4. **Given** code mode with an unverified model, **When** a write action is attempted, **Then** it is refused consistently with the platform's verification gate.

---

### User Story 5 - Files and reasoning are handled properly (Priority: P3)

A user uploads a file and uses it in conversation, and sees a reasoning model's thinking separately from its answer.

**Why this priority**: Both materially improve the experience but neither blocks the core loop.

**Independent Test**: Upload a file, reference it, and converse with a reasoning model.

**Acceptance Scenarios**:

1. **Given** an uploaded file, **When** it is attached, **Then** it is available within that conversation and its content is used within the model's context limit.
2. **Given** a file too large for context, **When** it is used, **Then** the limitation is stated rather than silently truncating without notice.
3. **Given** a reasoning model, **When** it responds, **Then** reasoning is shown separately and collapsed by default, never merged into the answer.
4. **Given** an unsupported file type, **When** upload is attempted, **Then** it is rejected with the supported types named.

---

### Edge Cases

- The stream drops mid-response: the interface MUST show what was received and offer to continue or retry, rather than discarding the partial response.
- The user navigates away mid-stream: the request MUST be cancelled so it does not generate for nobody.
- A model is evicted between picker display and message send: the interface MUST handle the resulting warm-up transparently.
- A conversation grows beyond the model's context: the interface MUST make the limit and its handling visible rather than silently dropping history.
- The user's entitlements change mid-session: the picker MUST reflect it on next open and a now-forbidden model MUST be refused.
- Two tabs hold the same conversation: both MUST remain consistent rather than overwriting each other.
- A very long response arrives: rendering MUST stay responsive.
- The user is idle long enough for their session to expire: they MUST be prompted to re-authenticate without losing draft input.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The UI MUST provide a conversation surface with incrementally streamed responses.
- **FR-002**: The model picker MUST show only published, `ready` models the user is entitled to, grouped by task tag.
- **FR-003**: Each picker entry MUST show display name, one-line description, tags, context size, size class, and live load state.
- **FR-004**: Cold models MUST show an estimated warm-up time before selection.
- **FR-005**: The UI MUST show warm-up progress while a model loads and begin streaming automatically when it completes.
- **FR-006**: A failed load MUST be presented clearly with a suggested next step.
- **FR-007**: The UI MUST NOT expose placement policies, variants, node identities, or internal identifiers to users.
- **FR-008**: Users MUST be able to switch models mid-conversation, and the switch MUST be visible in the transcript.
- **FR-009**: Conversations MUST persist per user, be listed most recent first, be reopenable, and be deletable.
- **FR-010**: Each message MUST record and display the model that produced it.
- **FR-011**: A user MUST NOT be able to access another user's conversations.
- **FR-012**: The UI MUST provide a code mode backed by the coding agent profile, executing as a sandboxed agent run.
- **FR-013**: Code-mode tool activity MUST be visible as it happens, and the user MUST be able to stop the run, invalidating its credential.
- **FR-014**: The UI MUST support file upload, attaching files to a conversation and making their content available within context limits.
- **FR-015**: Content that exceeds the context limit MUST be reported rather than silently truncated.
- **FR-016**: Unsupported file types MUST be rejected with the supported types named.
- **FR-017**: Reasoning blocks MUST be displayed separately from answers, collapsed by default, and never merged into answer text.
- **FR-018**: A dropped stream MUST preserve the partial response and offer continuation or retry.
- **FR-019**: Navigating away mid-stream MUST cancel the request.
- **FR-020**: An expired session MUST prompt re-authentication without losing draft input.
- **FR-021**: The UI MUST remain responsive while rendering long responses.

### Key Entities

- **Conversation**: A user's thread. Owner, title, messages, created and updated timestamps, mode.
- **Message**: One turn. Role, content, model used, reasoning block, attachments, token usage, timestamp.
- **Attachment**: An uploaded file. Owner, conversation, filename, type, size, storage reference, uploaded-at.
- **Picker Entry**: A user-facing model summary. Display name, description, tags, context size, size class, load state, warm-up estimate, task group.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A non-technical user chats, switches models, and understands when a model is loading, without assistance.
- **SC-002**: The picker never shows a model the user is not entitled to, and never shows an unpublished or non-ready model.
- **SC-003**: A cold-model request communicates warm-up within 1 second of send and begins streaming automatically on completion.
- **SC-004**: Conversations survive reload with full history and per-message model attribution.
- **SC-005**: A user cannot access another user's conversation, in 100% of attempts.
- **SC-006**: Code-mode requests execute as sandboxed agent runs and are stoppable within 5 seconds.
- **SC-007**: Reasoning content never appears merged into answer text.
- **SC-008**: A dropped stream never loses the already-received portion of a response.

## Assumptions

- Features 001–011 have shipped: the gateway with streaming and wait behaviour, the registry's user-facing curation fields, credentials, the console shell, and sandboxed runs for code mode.
- This feature extends the same SPA and ingress the admin console established in feature 008; it is not a separate application.
- Retrieval over uploaded files for the general agent requires an embedding model and is explicitly backlog; this feature covers upload, attachment, and in-context use only.
- Image generation appears in the UI with feature 015 and extends this surface rather than replacing it.
- Feedback capture — thumbs, regenerate-and-compare — is feature 018 and is deliberately not in this feature, though the message model must be able to accommodate it.
- Streaming passes through the tunnel and nginx with buffering disabled, established in feature 000.
- Warm-up estimates come from recorded load durations produced by features 004 and 009.
