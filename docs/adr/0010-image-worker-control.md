# ADR 0010: Use a dedicated Studio image worker control route

## Context

Feature 015 initially proposed extending the generic language engine start, status and stop routes for image generation. The image worker has a different lifecycle: one fenced native process per node, an offline image model copy, a private loopback health endpoint, and generation jobs after readiness. Reusing the language engine wire shape would blur those distinct commands and allow callers to supply fields the image worker does not accept.

## Decision

coire-node exposes typed `PUT /node/images/worker`, `GET /node/images/worker/{instance_id}` and `DELETE /node/images/worker/{instance_id}` on its authenticated control listeners. The data listener does not mount them. The existing node bearer protects each call; coire-node alone owns the worker process and its private loopback port. The API and scheduler will use these node routes through their existing scoped control path.

Language engines and the image worker share one reentrant admission lock and count each other's reservations against the node memory budget. The node reservation ledger reports their combined commitment. A restarted node agent reads the private process record before serving requests; uncertain records hold the full image budget until an operator reconciles them.

## Consequences

The worker contract stays explicit and versioned in `coire-core`. Image job routes and control-plane dispatch remain separate follow-up work. The process can be observed and stopped without exposing its loopback credential or engine port. This complies with constitution principles I, II, III, IV, V and VI; local contract tests and later Studio acceptance cover VII.
