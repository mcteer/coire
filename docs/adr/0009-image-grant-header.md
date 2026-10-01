# ADR 0009: Keep image grants out of request URLs

**Status:** Accepted for feature 015 implementation
**Date:** 2026-09-30

## Context

The initial image contract placed the download grant in a query parameter. The installed
OpenTelemetry ASGI instrumentation builds `http.url` from the incoming query string before
application code or a request hook can redact it. Even with Uvicorn and Nginx access logs
disabled, this would put an opaque bearer grant in telemetry. Principle IV requires zero
implicit trust, and image output access is private owner data.

## Decision

`ImageDownloadGrant.url` carries the token in a URL fragment (`#grant=...`). A client
parses the fragment locally, requests the path without a query string, and supplies the
token in `X-Coire-Image-Grant` alongside its normal credentials. The content route requires
both. The database stores only a hash bound to the current user or personal key and its
credential version. Grants expire within five minutes and explicit access is rechecked
at redemption. The response and content are `private, no-store`.

## Consequences

The web client must use the shared API helper to parse the fragment and attach the header;
it must not navigate directly to the fragment URL. Header capture by telemetry remains
disabled; do not add `X-Coire-Image-Grant` to captured-header allowlists. The parent
contract is updated from query parameter to header. No engine, network or container policy
changes are needed.
