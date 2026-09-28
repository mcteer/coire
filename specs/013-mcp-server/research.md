# Research: MCP Server

## R1 — Protocol and server library

**Decision**: Use the official Python MCP SDK in the existing `coire-mcp` image. Serve 2026-07-28 Streamable HTTP requests without protocol sessions and accept older 2025 clients through the SDK's stateless compatibility path. Mount the SDK ASGI app in the existing FastAPI host lifespan and expose only `/mcp` through nginx. Define tool input/output payloads as strict `coire-core` Pydantic models.

**Rationale**: The [2026-07-28 transport](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx) is request-scoped. The [official Python SDK](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/asgi.md) handles both protocol eras and documents ASGI lifespan mounting. Protocol compatibility is a poor place to maintain hand-written JSON-RPC.

**Alternatives**: Hand-written protocol handling duplicates version negotiation and cancellation rules. A stateful legacy session store adds idle memory and sticky routing without a feature requirement.

## R2 — Authentication and cancellation

**Decision**: Validate bearer API key, MCP scope, identity, origin, and request size at the HTTP boundary on every request. The MCP handler calls an owner-scoped internal run API; it does not hold scheduler or node credentials. On response disconnect, cancel the associated run through the existing kill/revoke lifecycle and confirm termination.

**Rationale**: [MCP authorization](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/authorization/index.mdx) is per request; the modern transport requires prompt cancellation after a disconnect. The current admin kill path already stops a Studio run and revokes its token.

**Alternatives**: Detached work would violate FR-012. Relying on protocol-session cleanup does not cover modern stateless requests.

## R3 — Workspace preparation and egress

**Decision**: After placement, coire-node prepares a unique per-run clone under its configured workspace root, from an HTTPS repository on a configured source-host allowlist or an owner-authorized registered workspace. Use explicit subprocess argv, size/time limits, pinned revision, no submodules/hooks, and a destination generated from the run ID. Write the typed harness request before container create and remove the workspace after artifact collection/retention. The run container remains on its internal network with only its gateway relay.

**Rationale**: The existing node already checks direct-child workspace paths and mounts them into runs. Neither the MCP service on core nor the model-driven container should clone. The node host can prepare the clone without opening runner egress.

**Alternatives**: Core cloning moves repository bytes through the 24 GB control node and needs cross-host workspace transfer. Runner cloning requires internet egress and violates FR-010. Arbitrary URLs permit internal-network fetches.

## R4 — Three task modes and verification

**Decision**: Extend run admission with a typed task class. Research and plan use a read-only repository mount and only bounded read/search tools; all modes write the structured result to a separate bounded output mount. Apply uses read/write tools and requires a published harness-verified variant. Tool implementations and final output validators live in the Studio harness. Entitlement checks remain in the API for all modes.

**Rationale**: Current `create_run` requires verification even for reads, while the feature spec permits unverified research/plan. Current coding tool names are declarations without implementations. The write gate must be enforced before scheduling and again in the harness.

**Alternatives**: A separate MCP-only runner would duplicate resource accounting, token revocation, and kill semantics. Allowing apply through the read path would weaken Principle V.

## R5 — Branch delivery and failure semantics

**Decision**: Apply creates a generated feature branch and commits locally. A branch artifact of at most 64 MiB is collected via the node and made owner-downloadable for seven days; the result also includes branch name, base revision, a diff excerpt of at most 1 MiB with an explicit truncation flag, and test summary. The platform never pushes to a default branch. Failing tests still produce the branch artifact. If artifact collection fails or exceeds the cap, report an infrastructure failure and retain the Studio clone for bounded recovery rather than claiming a usable download.

**Rationale**: A branch that exists only in a transient Studio clone is not useful to an editor user. A downloadable artifact makes the branch reviewable without granting a broad repository write credential to a container.

**Alternatives**: Automatic remote branch pushing needs repository write credentials and branch protection design outside the minimum feature. Returning a branch name alone loses the work after cleanup.

## R6 — Runtime footprint

**Decision**: Keep `coire-mcp` in the optional compose profile and one process. Use run-state notifications or bounded wait rather than an MCP background poller. Store bounded result metadata in Postgres; keep repository and artifact bytes outside it. Add one baseline metrics panel and alert with the existing observability stack.

**Rationale**: The Mini has 24 GB RAM and the efficiency review removed redundant idle work. Feature 013 adds request-time work but no always-on supporting service.

**Alternatives**: A workspace database, queue, or second MCP worker would add an owner and idle cost without a concrete scaling requirement.

## R7 — Test execution

**Decision**: Discover tests from recognized manifests and run only structured argv from an allowlist of executables present in the hardened agent image, initially Python pytest. Enforce timeout, CPU/memory/PID/output limits inherited from the run. Report one of `passed`, `failed`, `not_found`, or `unsupported` and include the discovered command. Never invoke a shell or execute repository-provided setup scripts during discovery.

**Rationale**: Arbitrary repositories can declare commands that need unavailable runtimes or execute untrusted scripts. The spec's test summary must distinguish that case from a passing or missing suite.

**Alternatives**: Shipping broad language toolchains in the agent image increases image size and attack surface. Executing arbitrary manifest strings through a shell loses the argv and executable allowlist boundary.
