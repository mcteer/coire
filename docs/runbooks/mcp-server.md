# MCP coding service

Coire exposes exactly three Streamable HTTP tools at `/mcp`: `research`, `plan`, and `apply`.
The service runs in the optional `mcp` Compose profile on core. Each call becomes an ordinary
Studio AgentRun; core does not run a harness or model. The Studio node prepares a fresh HTTPS
clone before starting an isolated run container. `apply` commits to a new `coire/*` branch and
returns its diff and test summary even when discovered tests fail. No tool pushes a branch.

## Start and check

Start with `COMPOSE_PROFILES=mcp deploy/compose/coire-up` using the normal release flow. The MCP
service and scheduler must both be healthy. Check `GET /ready` on the internal service and
`POST /mcp` through the public nginx address with a user-bound API key that has the `mcp`
scope. Browser `Origin` headers are refused. The external nginx route is exactly `/mcp`;
`/mcp/*` is unavailable.
An MCP request has a 20 minute wall-clock budget including placement and repository preparation;
the Studio container has its separately enforced run timeout.

The Studio node must have Git, a writable `RUN_WORKSPACE_ROOT`, and an agent image pinned by
digest in `RUN_AGENT_IMAGE`. `MCP_SOURCE_HOSTS` defaults to `github.com`; add only reviewed
HTTPS Git hosts. Clone and preparation are capped by `MCP_WORKSPACE_MAX_BYTES` (512 MiB) and
`MCP_WORKSPACE_PREPARE_TIMEOUT_S` (120). The API and node use the same allowlist. The artifact
retention window is `MCP_ARTIFACT_RETENTION_HOURS` (168, maximum 720).

## See a call and retrieve a branch

Find the call's `run_id` in the MCP response. The owner can query
`GET /api/v1/runs/{run_id}` or list `/api/v1/runs`; both include the tool, task class, state,
duration, and outcome. The dashboard **Coire Container Runs** shows MCP call outcomes and
workspace cleanup. Alerts `CoireMcpCallFailures` and `CoireMcpWorkspaceCleanupFailures` fire
on failures. Structured logs include the call ID, run ID, owner, model, and node at their
respective boundaries.

An apply result includes `artifact_id` and `artifact_url`. The owner can first read
`GET /api/v1/mcp/artifacts/{artifact_id}/metadata`, then download the `.bundle` from the
artifact URL using the same authenticated identity. Check `X-Coire-Sha256` against the
metadata. The API streams from the Studio; it does not retain a 64 MiB bundle on core. In a
local checkout, import the branch with `git fetch ./branch.bundle <branch-name>` and inspect
it before merging. An expired or different user's artifact returns 404.

## Kill, cleanup, and rollback

An administrator kills a running call with
`DELETE /api/v1/admin/runs/{run_id}` and a `RunKillRequest` reason. The run token is revoked
before the Studio kill command. A disconnected MCP call uses the same kill path. Research and
plan clones are removed after one hour; failed collection clones are retained for 24 hours
for investigation; successful apply bundles and clones stay until the configured artifact
expiry. The scheduler sweep runs every five minutes. A failed cleanup remains queued for a
later sweep and triggers an alert. If needed, use the authenticated node
`POST /node/workspaces/{run_id}/cleanup` after the container has stopped.

To roll back the MCP service, stop the `mcp` profile or restore the previous release. Chat
and admin API traffic keep using `coire-api`. Do not delete the Studio workspace while a
branch artifact is still within its owner download window.
