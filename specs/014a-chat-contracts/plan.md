# Implementation Plan: Chat Contracts

**Branch**: `feat/014a-chat-contracts` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add `conversation.py`, `chat.py`, and `files.py` to `coire-core`, with strict Pydantic v2 wire types and bounds matching the parent [data model](../014-chat-web-ui/data-model.md) and [contract](../014-chat-web-ui/contracts/chat-api.md). Keep this child below the review-size threshold; compatibility extensions to existing registry, gateway, node and harness contracts belong to the next child. Test contracts before implementation. No dependency, migration, route, image or engine change is part of this child.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Contracts only; no engine or container behavior. |
| III | Shared strict Pydantic wire shapes are the deliverable. |
| IV | Ownership fields derive from authenticated server identity in later API work; no caller owner field on create requests. |
| V | Model selection is an opaque registry UUID, with no raw path. |
| VI | Later runtime paths in parent 014 carry telemetry; no runtime path here. |
| VII | Core contract tests and full baseline gates run before child completion. |

No constitution exception. No dependencies or migration.
