# Feature Specification: Same-Tab Chat Draft Recovery

**Feature Branch**: feat/014o-chat-session-drafts  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-O1: A text draft and eligible model selection survive reload and ordinary authentication expiry in the same tab, scoped to the returning user identity and conversation ID.
- FR-O2: An identity change in the same tab removes the previous identity's draft store before showing a new user's Chat.
- FR-O3: Session storage contains only bounded text and model IDs, never API keys, bearer tokens, file bytes, response content, or server paths.
- FR-O4: Accepted sends clear their draft. A failed admission retains it; a different saved conversation never receives it.

## Independent acceptance

Browser tests cover reload/owner change, model selection, failed-send preservation, accepted-send cleanup and storage bounds. Existing web gates pass.
