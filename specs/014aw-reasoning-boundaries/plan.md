# Implementation Plan: Reasoning Boundary Regression Coverage

**Branch**: `feat/014aw-reasoning-boundaries` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the existing parser and mocked native stream tests with mixed-frame, direct-channel and Stop-at-marker cases. Keep the production parser and browser contract unchanged; record the focused gate in the parent review.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Verify the existing typed answer/reasoning delta contract. |
| IV | Stop retains the owner-controlled terminal path and does not expose held text. |
| VII | Focused regression tests run without an engine or Studio. |

No constitution exception or dependency.
