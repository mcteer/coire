# Implementation Plan: Continue a Partial Chat Response

**Branch**: `feat/014at-chat-continue` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add an explicit `recovery_mode` to the core turn request/projection and persist it through a reversible additive migration. Reuse the owner-scoped locked recovery admission and request-ID replay path. Retry continues to reuse its original input; continuation requires the fixed instruction, no attachments and saved nonblank partial text, then creates a new user/assistant pair. Compile history with the saved partial before the continuation instruction. Expose a separate browser action while leaving the editor draft intact. Keep native Chat disabled until the remaining parent gates pass.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Core wire contract first; regenerate OpenAPI and browser types. |
| IV | Owner-scoped latest-turn admission, immutable instruction and request-ID replay. |
| V | Recheck selected model eligibility for the new turn. |
| VI | Existing admission, stream and terminal spans/counters cover continuation. |
| VII | Contract and browser tests, reversible migration, full suites and local image gates. |

No constitution exception or new dependency.
