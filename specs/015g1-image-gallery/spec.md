# Feature Specification: Private image gallery metadata

**Feature Branch**: `feat/015g1-image-gallery`
**Parent**: `specs/015-image-generation/` (part of T022, T023, T043, T044)
**Dependency**: draft PR #62

## Goal

Expose only an owner's published output metadata through a bounded, stable gallery and individual record route. The route returns canonical saved recipes for reuse, never a blob path or unauthenticated content URL.

## Acceptance

1. GET `/api/v1/image-outputs` returns at most 100 owner-published, nondeleted rows, newest first, with a stable cursor and optional content tag. Unknown and explicit tags are visible only in the owner's private gallery.
2. GET `/api/v1/image-outputs/{id}` returns one owner-published, nondeleted output. Ordinary admins do not see another user's row.
3. Both routes require the live image principal guard. Unpublished, deleted, other-owner and invalid cursor rows yield a safe not-found response.
4. Responses project the strict shared `ImageOutput` shape without blob keys or classifier internals. Content and grants remain separate work.
