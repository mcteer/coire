# Feature Specification: Chat Reauthentication Prompt

**Feature Branch**: `feat/014ar-chat-reauth`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AR1: A 401 while loading Chat, sending, modifying files/history or observing events presents a clear same-origin sign-in action instead of a generic error or silent reconnect loop.
- FR-AR2: Expiry retains same-tab draft text/model/file selections for a returning account. A different returning owner clears the previous owner's draft store through the existing owner-keyed codec.
- FR-AR3: The observer stops reconnecting after an authentication refusal and never replays a generation POST. The initial `/me` check offers the same sign-in action.
