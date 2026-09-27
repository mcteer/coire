# Data Model: MCP Server

All wire shapes below are strict Pydantic models in `coire-core`; persistence rows mirror their stable identities.

| Entity | Fields and validation | Owner and lifecycle |
|---|---|---|
| `McpCall` | ID, tool enum (`research`, `plan`, `apply`), caller user/key IDs, model registry ID, source reference, base revision, run ID, state, timestamps, failure code | Created after auth; one run per call; terminal result retained for configured period |
| `WorkspaceSource` | HTTPS repository URL on source-host allowlist or registered workspace ID; requested revision | Caller must own registered source; URL is canonicalized before use |
| `WorkspacePrepare` | Run ID, source, immutable revision, task class, typed harness request, byte/time ceilings | Scheduler-authored node command; unique destination derived from run ID |
| `ResearchResult` | Findings with repository-relative path and positive line number, answer, run ID, source revision | Immutable read result; no workspace mutation |
| `PlanResult` | Goal, ordered steps, acceptance criteria, optional research result ID, run ID, source revision | Immutable; later calls resolve ID only for same owner and before expiry |
| `ApplyResult` | Generated branch, base and head revision, committed diff excerpt (≤1 MiB), truncation flag, test status (`passed/failed/not_found/unsupported`), counts, artifact ID, run ID | Returned even on test failure; artifact downloadable only by owner until expiry |
| `BranchArtifact` | ID, owner, run ID, content digest, byte size (≤64 MiB), storage reference, expiry, collected timestamp | Immutable; expires after seven days; never stored as raw Git bytes in Postgres |

## State transitions

`McpCall`: accepted → preparing → queued → running → collecting → succeeded or failed/timed_out/cancelled. A disconnect requests cancellation and must reach a terminal killed state. A failed clone ends before run creation with a distinct infrastructure code. A failing test is a successful apply call whose `test_status` is `failed`.

## Validation and isolation

- Source URL must use HTTPS, no embedded credentials, no local/private address resolution, and an allowed host; node rechecks resolved endpoints before clone.
- Workspace destination is a generated direct child of the configured Studio root; a caller cannot supply filesystem paths.
- Research and plan repository mounts are read only; apply repository mounts read/write. Every mode writes `result.json` to a distinct bounded output mount, not into the repository. Concurrent calls receive different clone directories and branch names.
- Test discovery emits structured argv for available, allowlisted runners. Found tests with no supported runner produce `unsupported`; absent tests produce `not_found`.
- The API chooses the model variant from registry records and enforces entitlement for all calls and harness verification for apply.
- Diff, test output, result, and artifact sizes are independently bounded. Truncation never changes the committed branch.
