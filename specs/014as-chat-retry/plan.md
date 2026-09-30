# Implementation Plan: Explicit Retry of Interrupted Chat

**Branch**: `feat/014as-chat-retry` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Project the existing `retry_of` relation in the core turn contract. Under the conversation admission lock, require the referenced terminal turn's assistant message to be the latest message, and require unchanged original text/selected files/prompt snapshot. Reauthorize the chosen model and files and preflight context. Reuse the input message ID, create a new assistant/turn/event, and rely on the existing request-ID replay path. For subsequent turns, select the most recent assistant attempt by message position for each input before compiling the prompt. The browser keeps turns from owner detail, offers a separate Retry action, and appends only the new assistant bubble on accepted retry.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Core turn projection before API/browser; refreshed OpenAPI and TypeScript. |
| IV | Locked owner conversation, latest-turn and immutable-input checks; no replay side effect. |
| V | Model and file eligibility rechecked for every retry. |
| VI | Existing admission/stream/terminal spans and counters apply. |
| VII | API/web contracts, full suites and API image gates. |

No constitution exception or dependency.
