# Implementation Plan: Versioned Chat History Edit

**Branch**: feat/014v-chat-history-edit | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use the shared `ChatConversationUpdate` contract. Lock the owner conversation, check revision and active-turn constraints, validate selected-model eligibility, and atomically persist the revised metadata and observer event. Expose PATCH behind the existing Chat auth/Origin guard. The history drawer sends the saved revision with its title and retains the edit after refusal; on conflict the list is refreshed to obtain the latest revision.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Shared typed update/event models and regenerated OpenAPI/browser types. |
| IV | Owner lock, eligibility and exact browser Origin. |
| VI | Route span/counter with content-free IDs and existing Chat dashboard/alert. |
| VII | Service, route and browser interaction tests. |

No constitution exception or dependency.
