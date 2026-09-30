# Feature Specification: Private File Previews

**Feature Branch**: `feat/014ah-file-previews`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AH1: Ready attachments expose only typed generated preview metadata (asset ID, page, dimensions, media type); original bytes remain available only through the owner-scoped download route.
- FR-AH2: An authenticated, owner and conversation bound preview route serves normalized PNG bytes only for an asset in the attachment's committed ready manifest. Every read rechecks generated keys, size, digest and dimensions and refuses missing or altered output.
- FR-AH3: Preview responses are inline PNG with `nosniff`, `no-store`, sandboxed content policy and same-origin resource policy. PDFs are never embedded as original documents and no URL contains a bearer token.
- FR-AH4: Contract tests cover owner/foreign/cross-parent/deleted/unknown asset denial, safe headers and tampered bytes. Native Chat remains default-off until processing controls, UI and purge are complete.
