# MCP and Internal Contracts

## Public Streamable HTTP

The nginx route `/mcp` forwards to the independent MCP service. It accepts the official MCP 2026-07-28 request envelope and stateless older client requests supported by the pinned SDK. Every request requires a Coire bearer API key with `mcp` scope and a bound user identity. Auth failure is HTTP 401; insufficient scope or source/model entitlement is HTTP 403. The SDK handles JSON-RPC protocol errors. All application tool arguments and structured results validate against `coire-core` Pydantic models.

`tools/list` returns exactly `research`, `plan`, and `apply`:

| Tool | Input | Structured result |
|---|---|---|
| `research` | `source` (allowed HTTPS URL or registered ID), `revision`, `question`, optional registry `model_id` | `result_id`, `run_id`, answer, path/line citations, source revision |
| `plan` | `source`, `revision`, `goal`, optional same-owner `research_result_id`, optional `model_id` | `result_id`, `run_id`, ordered steps, acceptance criteria, source revision |
| `apply` | `source`, `revision`, `plan_result_id` or explicit plan, optional `model_id` | `run_id`, branch, base/head revisions, diff excerpt (≤1 MiB), `diff_truncated`, test status (`passed/failed/not_found/unsupported`)/counts, artifact ID |

All tools return explicit error codes for clone failure, model refusal, timeout, cancellation, agent failure, and artifact collection failure. A test failure remains a valid `apply` result with failing test status. An unavailable discovered runner is `unsupported`; it is not a pass. Artifacts are capped at 64 MiB and owner-downloadable for seven days. Oversized collection is an infrastructure error with bounded Studio clone retention for recovery. Only an owner can reuse result IDs. No chat, image, admin, resource, prompt, or extra tool is advertised.

## Internal boundaries

- MCP → API: typed owner-scoped create, await, cancel, and result retrieval. The API records attribution and audit, performs model admission and entitlement, and owns run state.
- API/scheduler → node: typed prepare/cleanup commands with run ID, approved source, revision, task class, size/time bounds, and `HarnessRunRequest`. The node creates a read-only or read/write repository mount plus a separate bounded output mount. No raw git command string or caller path crosses this boundary.
- Node → API: typed prepare status and collected result/artifact metadata. Artifact download is owner-scoped and streamed with digest and size checks.
- API → web generated types: regenerate OpenAPI and `schema.d.ts` if artifact or result REST routes are added. `/v1` OpenAI-compatible shapes remain unchanged.

## Compatibility

The existing run create contract gains additive optional task class and prepared-workspace fields with current behavior preserved for older callers. New internal node commands are versioned with the node API. A schema change requires contract tests and a migration note. Protocol version negotiation belongs to the official MCP SDK.
