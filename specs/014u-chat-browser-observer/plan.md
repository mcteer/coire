# Implementation Plan: Browser Conversation Observer

**Branch**: feat/014u-chat-browser-observer | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the shared SSE hook module with a GET-only observer. It uses the existing incremental parser, validates the scoped cursor, reconnects with bounded backoff and stops on authorization/absence responses. The selected conversation keeps its last event cursor; while the local POST owns generation, the observer disconnects. Observer notifications trigger a bounded saved-detail refresh rather than another POST, and selection generations discard stale responses.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Browser events and responses use generated Chat types. |
| IV | GET observer has no mutation authority; server checks ownership. |
| VI | Server observer admission telemetry remains visible. |
| VII | Hook and two-tab style page tests plus web gates. |

No constitution exception or dependency.
