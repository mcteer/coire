# Feature Specification: Chat Authorization and Eligibility

**Feature Branch**: `feat/014e-chat-authorization`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-E1: Native Chat accepts only a verified Access user identity or a user-owned API key with `chat` scope; anonymous, run, ops, node, service and unowned legacy admin identities are refused.
- FR-E2: A cookie/Access browser mutation requires an exact configured same-origin `Origin`; read operations and scoped bearer API-key mutations do not depend on browser origin. No CORS widening occurs.
- FR-E3: Conversation ownership applies to admins and API keys. Missing, foreign and deleted conversations yield the same 404 response.
- FR-E4: Published, ready model eligibility uses actual entitlements for nonadmins in existing `/api/v1/models` and `/v1` routes. Chat eligibility applies the published/ready/entitled rule to every caller. Existing deliberate admin discovery behavior outside Chat stays intact.
- FR-E5: No acquisition or model path is exposed through eligibility checks.

## Independent acceptance

Guard tests cover every principal kind, exact Origin, foreign/deleted owner, and API-key scope. Listing/resolution tests cover entitled and unentitled models plus existing admin behavior; existing auth/gateway/admin tests stay green. Actual chat routes follow later children.
