# Implementation Plan: Same-Tab Chat Draft Recovery

**Branch**: feat/014o-chat-session-drafts | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Pass the verified current user ID from App into Chat and the conversation hook. A small storage helper holds up to 20 entries under a user-specific session key, each with at most 64 KiB of text and an optional model UUID. A separate current-owner sentinel clears the previous owner's key on identity change. The hook hydrates the new-conversation draft, saves text/model edits, restores per-conversation values after detail reads, and clears an accepted draft. Storage failure does not block chat. File selections and explicit logout integration follow their own feature paths.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Browser-only draft state; no inference changes. |
| III | No new wire shape; existing generated model IDs. |
| IV | Verified user identity scopes session data; no credential material. |
| V | Restored model IDs must still appear in the eligible picker. |
| VI | No new server path. |
| VII | Browser tests for restoration, isolation and bounds. |

No constitution exception or dependency.
