# Feature Specification: Chat Contracts

**Feature Branch**: `feat/014a-chat-contracts`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Purpose

Provide strict shared wire contracts for persistent, owner-scoped chat and bounded attachment processing. This child delivers types only; the parent specification governs later behavior and acceptance. Existing text `/v1`, model registry, and harness requests remain valid.

## Requirements

- FR-A1: A canonical Conversation contains ordered role/content parts and stable message IDs. Native chat metadata contains opaque IDs, revision, ownership-derived fields, model attribution, bounded text, turn state, and terminal usage.
- FR-A2: Native requests and responses reject unknown fields, overlong content, invalid page selections, and unbounded list sizes. A turn request carries an idempotency key and expected revision.
- FR-A3: Events have discriminated types and bounded payloads for acceptance, status, deltas, terminal, attachment changes, and conversation deletion. Cursor IDs are monotonic within one conversation.
- FR-A4: File processing request/result/status types use ULID job IDs, generated asset IDs, SHA-256 digests, selected pages, safe metadata, and bounded sizes. They expose no caller-supplied filesystem path.
- FR-A5: No new schema enables a user to choose an engine path or acquire a model. All existing gateway and harness contract tests stay green.

## Independent acceptance

Core contract tests validate legal text and visual selections, ownership metadata, event discrimination, worker bounds, unknown-field rejection, and legacy `/v1` text/null request roundtrips. Ruff, strict mypy, and the complete core test suite pass.
