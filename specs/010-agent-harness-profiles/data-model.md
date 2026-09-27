# Data Model: Agent Harness

## AgentProfile

Name (`coding|general|image|ops`), system prompt revision, output contract, ≤10 base tools, optional
tool packs, model tag preferences, sampling settings, stop sequences, and write capability.

## CapabilityStrategy

Tool mode (`native|json|delimited`), output mode (`native|json|delimited`), context window, reasoning
tags, thinking-token ceiling, parallel-tool support, and optional repository template reference.

## HarnessRun

Run ID, profile, variant ID, task class (`read|write`), immutable message history, transmitted context
view, reasoning blocks, validated result, retry events, truncations, status, and timing.

## HarnessEvaluation

ULID, variant ID, harness/engine versions, category scores (tool, structured, edit, long-context),
overall score, verdict (`passed|failed|infrastructure_error`), diagnostics, and run timestamp. Rows are
append-only. A passing row may set that exact variant verified; no other path may.

## Invariants

- User profiles cannot import or select ops tools.
- Write runs require an exact verified variant before model invocation.
- Every structured result is validated; retry/repair outputs use the same declared type.
- Stored conversation history is append-only; summaries alter only the next transmitted view.
- Infrastructure errors do not lower scores or change verification.
