# Implementation Plan: Safe Chat Code and Links

**Branch**: `feat/014av-chat-markdown` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Render fenced Markdown code through a small browser component that extracts visible code text and uses the Clipboard API on an explicit click. Tighten the existing URL transform so a protocol-relative URL cannot pass the local-path branch. Retain the image suppression and default escaped HTML behavior. Test the resulting DOM and clipboard text.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Browser presentation only; no new wire shape. |
| IV | Unsafe links/images stay inert and no content is sent to another service. |
| VI | No new server operation; existing Chat telemetry applies. |
| VII | Browser tests, lint, build and local web-image policy/scan. |

No constitution exception or dependency.
