# Feature Specification: Explicit Browser Sign-out

**Feature Branch**: `feat/014bg-chat-logout`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

An authenticated Chat or admin user can explicitly sign out. The action clears only that verified owner's same-tab Chat drafts before navigating to Cloudflare Access's same-origin logout path. Session expiry continues to preserve drafts until the returning identity is verified; an identity change clears the prior owner's drafts. No browser draft contains credentials or file bytes.

Acceptance: the shell has a same-origin sign-out link, clicking it clears the owner's draft and owner marker, and existing expiry/identity tests remain green.
