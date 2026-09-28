# Feature 013 Validation

## Prerequisites

- Feature 013 images are built and pass image policy/CVE scan; `coire-mcp` optional profile is enabled.
- An MCP-scoped API key with a user identity, one published coding model, and one harness-verified coding variant exist.
- A tiny model (≤1 GB) is available for integration tests. Use a disposable sample Git repository on an allowed source host.
- Studio control path and node agent are healthy. Record any unavailable real-cluster gate; a skipped probe is not a pass.

## Contract and isolation checks

1. Run unit and contract tests, OpenAPI freshness, strict mypy, lint, and MCP client protocol tests. Enumerate tools and confirm exactly three names.
2. Call each tool without a credential and with a key lacking `mcp`; expect 401 and 403. Try chat/admin/image method names; expect no such tool.
3. Use an unverified published model for research and plan; expect read-only results. Use it for apply; expect a verification refusal before a run starts.
4. Attempt an unapproved URL, a local/private resolved address, path traversal, and two simultaneous applies to one source. Confirm refusal or isolated clones as applicable.
5. Disconnect during a long run, and separately use admin kill. Confirm the container stops and token is revoked within 5 seconds.

## End-to-end tiny-model loop

1. Connect an ordinary MCP client to `/mcp`; call research on a sample repository and verify cited path/line pairs against the checked-out revision.
2. Call plan using the research ID. Confirm ordered steps and acceptance criteria, and no file change after either read call.
3. Call apply with a verified model. Confirm a non-default branch, commit, bounded diff, test counts/status, and owner-downloadable branch artifact. Import the artifact into a separate clone and verify its head revision.
4. Repeat with deliberately failing tests. Confirm branch/diff/artifact remain available and test status is `failed`. Repeat with no tests and confirm `not_found`; use an unsupported test runtime and confirm `unsupported`, never `passed`.
5. Restart only `coire-mcp` during chat traffic and confirm chat continues. Inspect run listing, audit row, metric, span, dashboard panel, alert rule, and bounded cleanup.

Record commands, counts, CI links, and real-cluster evidence here during implementation. Do not mark acceptance complete on an unrun or skipped test.

## Local validation record (2026-09-27)

- `uv run ruff check .` and `uv run mypy apps packages`: passed (389 source files).
- `uv run pytest -q -m 'not integration'`: 790 passed, 8 skipped, 109 deselected.
- `uv run python -m coire_api.openapi --check`: passed.
- `pnpm -C apps/coire-web test`, `lint`, and `exec tsc --noEmit`: passed (19 web tests).
- `uv run alembic -c apps/coire-api/alembic.ini heads`: one head, `0014_mcp_calls`.
- Local arm64 agent and MCP images: build and image-policy passed. Trivy CRITICAL scans passed. The agent image ran Git and produced a branch bundle.
- `uv run pytest --collect-only -q tests/integration/test_mcp_loop.py`: one composed loop test collected.
- [CI run 36358492241](https://github.com/mcteer/coire/actions/runs/36358492241): all jobs passed; composed integration reported 107 passed and 2 skipped. The MCP loop proved cited research, prior-result plan, a committed branch, failing and missing test status, a downloadable/importable bundle, and two separate concurrent branches on the ready CI Studio. The CI engine is deterministic; a real tiny-model run remains pending.

## Follow-up composed validation (2026-09-27)

- `COIRE_INTEGRATION=1 uv run pytest -q tests/integration/test_mcp_lifecycle.py tests/integration/test_mcp_loop.py`: 6 passed in 323.36 seconds. This includes disconnect, timeout, admin kill, MCP-only restart with chat traffic, the original complete loop, and an unverified read/write gate followed by admin evaluation and successful apply.
- The disposable compose project used the acquired tiny-model files with its deterministic CI engine. A real tiny-model execution on a Studio is still required for T036.

## Studio attempt (2026-09-28)

- Branch `4598b84`: Ruff, mypy (389 files), OpenAPI freshness, web tests (19), ESLint, and TypeScript passed. Unit/contract pytest passed outside the sandbox: 790 passed, 8 skipped, 114 integration deselected. The sandboxed run could not execute 19 engine contract setups because macOS denied process enumeration; the unrestricted rerun passed.
- `coire-edge-a.lab` is reachable by SSH and holds the Qwen2.5 0.5B model files. Its node LaunchDaemon runs and returns 401 without node authentication.
- The 013 release is now live at migration `0014_mcp_calls`; the core gateway and MCP service are healthy. The current digest-pinned agent and relay images are loaded on edge-a, and the node wheel and LaunchDaemon are installed there. A temporary, audited admin API key was created through the authenticated admin API for this acceptance run.
- The tiny Qwen2.5 Coder 0.5B variant passed a real model validation on edge-b. Replication to edge-a initially failed three times with `All connection attempts failed`. Enabling Python in macOS Local Network and restarting the daemon did not resolve the data-link refusal. The operator then approved and applied Apple's Ethernet exception for only `192.168.100.12/32` and rebooted edge-a.
- After fixing the durable acquisition retry ID, the updated non-integration suite passed: 791 passed, 8 skipped, 114 integration deselected. Strict mypy passed for 389 files. The rebuilt scheduler passed image policy and a Trivy CRITICAL scan.

## Studio completion (2026-09-28)

- After the approved peer-address exception, the authenticated data-link probe reported `ip_state: up` at roughly 4 ms. Admin acquisition, real weight validation, and replication completed for both Qwen2.5 Coder 0.5B and 1.5B. The 1.5B repository contained 880,172,100 bytes; it met the ≤1 GB gate. Edge-a served the 1.5B variant from a real `mlx_lm.server` process.
- An ordinary MCP-only key enumerated exactly `research`, `plan`, and `apply`, and received 403 on chat/admin. The 1.5B unverified variant completed real research (`09e9ac94-8808-4819-8a2b-2ba672e42ee5`) citing `README:1` at revision `7fd1a60b01f91b314f59955a4e4d4e80d8edf11d`, and plan (`eca2786c-b07d-4d7d-9d9d-515182731a78`) with ordered steps and acceptance criteria. Apply was refused before verification with `no entitled harness-verified coding model is available`.
- The real 1.5B harness evaluation `e808bc6b-ddab-4f92-a92e-fa421da2807c` passed all four categories at 1.0 after fixing the probe's chat-template stop marker and repetitive long-context filler. The pass was recorded through the authenticated admin API, making the exact variant write-eligible.
- MCP apply run `32877c9d-996a-42c7-997c-c1bf9424db97` succeeded on edge-a. It committed `GREETING.md` on branch `coire/32877c9d996a-bcb4810a`; the head `f78af40ad5821c077c7443ecd35310c4083b14f5` has the expected base parent. The test status was `not_found`, accurately reflecting the sample repository. The owner downloaded artifact `41878f75-a336-418e-a6ed-86f060e320db` (922 bytes, SHA-256 `6e8dbff05d90d05ed2ff50ccaeebdb05ef9cb481c02ce46e7bf92b061327bbd1`); `git bundle verify` passed and a second clone fetched and inspected the commit.
- The disposable composed suite separately proved failing tests, concurrent applies, disconnect/kill/timeout, and MCP-only restart with chat traffic (6 passed). These cases were not repeated on the live Studio. The live node continues to log a registration-token mismatch (`not_issued`) although the core's authenticated control/probe calls and all three Studio runs succeeded; address that operational mismatch separately.
- Final local gates after the live fixes: `uv run ruff check .` passed; strict mypy passed for 390 source files; non-integration pytest reported 792 passed, 8 expected skips, and 114 deselected; OpenAPI freshness passed. Six skips require `COIRE_ENGINE=1` for separate local bare-engine tests; the other two exercise a non-Darwin fallback and a third-party image without a healthcheck. The real Studio MCP loop above supplies the Feature 013 tiny-model integration gate. Web tests passed (19), as did ESLint and TypeScript. The rebuilt agent digest passed all seven image-policy rules and a Trivy CRITICAL scan with zero findings.
