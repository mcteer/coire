# Feature 013 implementation record

## Dependencies

- `mcp==2.2.0` is the official Python MCP SDK, licensed MIT. It implements the current and legacy Streamable HTTP protocol versions, avoiding a custom JSON-RPC transport. Version is exact in `apps/coire-api/pyproject.toml` and `uv.lock`. Source: [official SDK release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0), [PyPI license](https://pypi.org/project/mcp/2.2.0/).

## Deployment inspection

The optional `coire-mcp` service has its own distroless image, non-root read-only runtime, dropped capabilities, 512 MiB memory cap, healthcheck, and separate compose profile. nginx routes only the exact `/mcp` endpoint.

## Validation

Ruff, mypy (389 source files), and the non-integration suite passed after OpenAPI regeneration (790 tests passed, 8 skipped, 109 integration deselected). The web suite passed (19 tests), as did ESLint and TypeScript. The local arm64 agent image built at 68.6 MB and passed all seven image-policy rules. Its Git 2.39.5 executable ran and created a branch bundle in the distroless runtime. The arm64 MCP image also passed image policy. Trivy CRITICAL scans passed for both images. [CI run 36358492241](https://github.com/mcteer/coire/actions/runs/36358492241) passed all jobs, including 107 composed integration tests with 2 skipped. The composed loop used a tiny-model acquisition and the deterministic CI fake engine. A real tiny-model run and the separate lifecycle and unverified-read composed cases remain pending.
# Coding runner dependencies

The Studio agent image adds `pytest==9.1.1` (MIT) so the initial allowlisted test runner
can execute discovered Python tests without a shell. It also ships the Git CLI from
Debian bookworm (`git=1:2.39.5-0+deb12u3`, GPL-2.0-only) for local branch, commit, diff, and bundle creation.
The CLI is a separate process and cannot reach a remote from the isolated run network;
the coding tool never invokes `push`. Both are required for the spec's reviewable apply
artifact and are absent from the core control-plane images.

## Follow-up acceptance evidence (2026-09-27)

The previously omitted composed tests now cover MCP disconnect, admin kill within five
seconds, run timeout, token revocation, MCP-only restart during chat traffic, and the
unverified-read/verified-write transition. The full targeted composed suite passed:
`COIRE_INTEGRATION=1 uv run pytest -q tests/integration/test_mcp_lifecycle.py
tests/integration/test_mcp_loop.py` — 6 passed in 323.36 seconds on the disposable local
compose project with the acquired tiny-model files and deterministic CI engine. The timeout
test exposed a relay that remained active after the run container stopped; the node now waits
for both containers to stop before reporting timeout. The disconnect test exposed that the
MCP JSON transport did not observe a dropped HTTP client; the outer ASGI receive monitor now
cancels the tool task. An apply refusal now includes the verification reason in the MCP tool
error instead of a generic crash.

The real tiny-model run against a Studio remains the final T036 gate. The 013 release and
MCP service were subsequently deployed; see the updated Studio record below.

Final local static and unit gates: Ruff passed, strict mypy passed for 389 source files,
`uv run pytest -q -m 'not integration'` reported 790 passed and 8 skipped, and the OpenAPI
freshness check passed. Web tests (19), lint, and TypeScript passed. The rebuilt arm64 API,
scheduler, migrate, agent, MCP, and web images passed image policy; Trivy reported zero
CRITICAL findings for those images. No dependencies were added by this follow-up.

## Studio acceptance attempt (2026-09-28)

At branch head `4598b84`, Ruff, strict mypy (389 files), OpenAPI freshness, web tests
(19), ESLint, and TypeScript passed. The unit/contract suite passed with 790 passed,
8 skipped, and 114 integration tests deselected when run outside the workspace sandbox;
inside the sandbox, 19 engine contract setups could not enumerate macOS processes due to
`sysctl` permission denial. This was an environment restriction, not a test failure.

The core stack was deployed at migration `0014_mcp_calls`; gateway and MCP health checks
pass. The current agent and relay images were loaded by digest on edge-a, and the new
coire-core/coire-node wheels and LaunchDaemon were installed. The legacy Keychain bearer
was used only against a temporary internal bootstrap API with legacy authentication
enabled to create an audited, scoped admin key through the admin API; the temporary API
was removed. The new key works on the production admin API.

The admin acquisition of `mlx-community/Qwen2.5-Coder-0.5B-Instruct-4bit` downloaded and
validated real weights on edge-b. Replication to edge-a failed twice with a connection
error. Fresh uv-managed Python processes initially had macOS Local Network access denied;
an ad-hoc signature with stable identity restored a direct data-fabric connection on
both Studios. The edge-a daemon still needs a root-owned LaunchDaemon restart to acquire
that identity. The installer now signs its uv-managed interpreter automatically. An
acquisition retry also exposed a DBOS workflow-ID collision; attempt-specific IDs now
allow actual reruns. Its targeted test, Ruff, strict mypy (389 files), 791 unit/contract
tests (8 skipped, 114 integration deselected), scheduler image policy, and Trivy CRITICAL
scan pass. A third replica attempt after a root-owned LaunchDaemon restart still failed:
the node's own data-link probe reports `[Errno 65] No route to host` for edge-b's
dedicated Ethernet bridge, while an SSH-launched process reaches it. The pending operator
decision is whether to enable a Coire/Python Local Network entry, if present, or approve
Apple's system-wide `AllowedEthernetLocalNetworkAddresses` exception for the dedicated
fabric. Enabling Python in Local Network and restarting the daemon left the probe at
`[Errno 65] No route to host`; approval has been requested for a single peer-address
exception (`192.168.100.12/32`). At that point no real-model MCP result had been obtained.

## T036 final evidence (2026-09-28)

The operator applied the approved `192.168.100.12/32` Ethernet exception and rebooted
edge-a. The Studio data-link probe became `up` at roughly 4 ms, enabling real admin
acquisition and replication. Qwen2.5 Coder 0.5B and 1.5B were validated and served;
the selected 1.5B variant contains 880,172,100 bytes of repository data. Its first
evaluation revealed that the CLI probe treated an MLX chat-template terminator as model
JSON and used 1,200 identical filler words, inducing repetition. The corrected probe
keeps the same four strict assertions and 1,200-item retrieval length. Real evaluation
`e808bc6b-ddab-4f92-a92e-fa421da2807c` passed every category at 1.0 and was
recorded through the audited admin API. The earlier failed evaluations remain in the
append-only scorecard history.

The live MCP endpoint listed exactly three tools under an ordinary MCP-only key, and
that key was refused for chat and admin. Real research run
`09e9ac94-8808-4819-8a2b-2ba672e42ee5` and plan run
`eca2786c-b07d-4d7d-9d9d-515182731a78` succeeded on edge-a using the unverified
model; apply was refused before verification. After verification, apply run
`32877c9d-996a-42c7-997c-c1bf9424db97` produced branch
`coire/32877c9d996a-bcb4810a`, commit
`f78af40ad5821c077c7443ecd35310c4083b14f5`, a bounded diff, and honest
`not_found` test status on the testless Hello-World repository. Owner download of
artifact `41878f75-a336-418e-a6ed-86f060e320db` succeeded. Its SHA-256 is
`6e8dbff05d90d05ed2ff50ccaeebdb05ef9cb481c02ce46e7bf92b061327bbd1`;
`git bundle verify` and fetch into a separate clone confirmed the expected parent and
the new `GREETING.md`. All three run records show the MCP owner, Studio node, tool, and
succeeded outcome. The 6-case disposable composed suite covers failing/missing tests,
concurrent branches, cancellation, timeout, and MCP-only restart; these were not rerun
on live hardware.

The final agent digest `coire-agent@sha256:d4c62ac5aa78a04be59609fe74013103cca39408e7a119054ab1b5773a4b4391`
passed all seven image-policy rules and a Trivy CRITICAL scan with zero findings. It is
loaded on edge-a and configured in its root-owned LaunchDaemon; the core stack uses the
same digest. The final API image was rebuilt, passed image policy and a zero-CRITICAL
Trivy scan, and was deployed with healthy API/MCP/scheduler status. The scheduler image was rebuilt, policy-checked, and scanned after the DBOS
retry fix. Earlier API, migrate, MCP, web, and relay images passed their build/policy/
CRITICAL gates. No new dependencies were added. The real node reports intermittent
registration `401 not_issued` for its legacy Keychain token even though authenticated
control, health, and Studio runs work; this is tracked as an operational follow-up.

Final local checks after the live fixes: `uv run ruff check .` passed; strict mypy
passed on 390 files; `uv run pytest -q -m 'not integration'` reported 792 passed,
8 expected skips, and 114 integration tests deselected; OpenAPI freshness passed.
Six skips are separate bare-engine tests requiring `COIRE_ENGINE=1`, while the real
Studio MCP loop above supplies this feature's tiny-model gate. One skip is a
non-Darwin fallback; the other is an upstream image without a healthcheck.
Web Vitest passed 19 tests, and ESLint and TypeScript passed.
