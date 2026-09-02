# Research: Admin Console

## Aggregate live-state transport

- **Decision**: One admin SSE endpoint emits a full initial snapshot, typed updates with monotonic IDs, and keep-alives. Reconnect sends the last ID; an expired cursor produces a reconcile snapshot.
- **Rationale**: This makes consistency/recovery testable and avoids many row streams.
- **Alternatives considered**: polling (forbidden), per-resource streams, WebSockets.

## Browser streaming authentication

- **Decision**: A fetch-based SSE parser shares credentials/problem handling with the API client.
- **Rationale**: Native EventSource cannot carry API-key headers and hides useful response errors.
- **Alternatives considered**: EventSource; query credentials (secret leakage).

## Pagination and concurrency

- **Decision**: Opaque cursor pages with stable timestamp/id ordering; integer entity versions required via `If-Match`, with RFC 9457 `409` on conflict.
- **Rationale**: Stable bounded lists and no silent overwrites across tabs/admins.
- **Alternatives considered**: offsets and last-write-wins.

## Shell and routing

- **Decision**: Semantic History API routing and reusable components, using `docs/design/tokens.css`, without a new UI/router dependency.
- **Rationale**: Seven shallow tabs do not warrant dependency/license overhead.
- **Alternatives considered**: React Router and a component library.

## Ask Coire

- **Decision**: A strict read-only question contract grounded in the current snapshot. It returns `unavailable` if the pinned Studio model path is down; no mutation/tool/confirmation shape exists.
- **Rationale**: Meets the safety boundary and Principle II while providing deterministic degradation.
- **Alternatives considered**: omission; future mutation proposals; local model fallback.

## Capability visibility and observability

- **Decision**: Render only server-advertised shipped capabilities. Trace `coire.api.console.{snapshot,stream,action,ask}`, count connections/reconnects/outcomes, and alert on stream failures/staleness.
- **Rationale**: Meets FR-020 and distinguishes frozen UI from stale node data.
- **Alternatives considered**: disabled placeholders and browser-only logs.
