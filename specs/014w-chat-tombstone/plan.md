# Implementation Plan: Immediate Chat Conversation Tombstone

**Branch**: feat/014w-chat-tombstone | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Lock the owner conversation, check revision and mark its deletion timestamp before returning. Mark any active plain-chat turn `stop_requested` so the controlling stream closes. Persist a content-free `conversation.deleted` event; existing observers emit it once and close. The browser uses the generated DELETE contract and a two-click confirmation, then removes saved UI state. A later maintenance child performs verified original/derived content purge within the returned deadline.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Shared delete request/result and event contracts; regenerated OpenAPI/browser types. |
| IV | Owner lock, exact Origin, immediate tombstone denial and active Stop. |
| VI | Delete span/counter and existing Chat dashboard/alert. |
| VII | Route/service, observer and browser confirmation tests. |

No constitution exception or dependency.
