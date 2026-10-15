# Implementation handoff: 020 control-plane failover

Updated 2026-10-15 on `feat/020-control-plane-failover`, based on merged 019 (`cd355554`).
Current state: the live Core/native success, exact manual rollback and failed-candidate
restoration matrix PASSED, including final metadata preservation and owned cleanup.
`coire-020-application-bb59eadcfb` has no running test containers. Both Studio test
supervisors, nodes and guards are stopped; owned frontends are removed. Volumes and
journals are retained. See the final success table in review.md and private results.
The older failed `304b0b9db7` cohort is suspended with unresolved historical holds;
its native prefix is not reusable. Supported Cloudflare resource bodies and local ingress policy/scope/isolation tests are
prepared. Owner reaffirmed on 2026-10-15: Cloudflare stays deferred; stop requesting
account setup, since the platform has not yet been used locally. Continue local
implementation, tests and PR validation. Do not treat provider prerequisites as blocking
this local work or resume the rollout without a later owner request. Physical fault
acceptance remains separately unqualified. No connector is installed on Core. Unrestricted `uv run pytest -q` finished with 6,359 passed, 445 skipped, 25 failed
and one setup error. Failures are isolated integration prerequisites: missing hosted-native
fixture/RUNNER_TEMP, absent controlled-stream API, stale local executor image, and missing
current composed cohort images. Report: `/tmp/coire-020-full-pytest-report.txt`. The first exact CI
unit selection had one one-second subprocess timeout; its unchanged seven-test file passed.
A later invocation used an in-repository temp root and correctly hit five external-path
security guards (6,095 other tests passed). With a unique external temp root, all 62 tests
in the affected deployment/report/command files pass. The final whole CI unit selection
now runs in session 39269, log `/tmp/coire-020-unit-external-final.log`, basetemp
`/tmp/coire-020-unit-external-final-20261015`. Never reuse a basetemp; pytest clears it.
Six corrected-image bootstrap integration cases pass. All 11 actual composed cohort and
observability cases pass (session 74218 completed), log `/tmp/coire-020-cohort-regression.log`.
Owned cohort containers are removed. Current images use private `020-regression` tags;
no production tag/container was replaced. Hosted native and controlled-stream fixtures
belong to their provisioned CI jobs.
Unrelated skill/workflow edits remain outside feature commits.

Power-interruption recovery on 2026-10-15: the production Core and isolated application cohort
`coire-020-application-35c13da42b` restarted healthy. Studio temporary qualification checkouts
were lost; both were restored from the current source with `uv sync --frozen --all-packages`.
Both Studio Docker daemons required `orb start`; the CLI succeeded without a GUI login.
Two empty, exactly labeled resource-collector networks left by the interruption were removed
after ownership and absence of attached containers were checked. Healthy-collector diagnostic
preflight now passes with actual metrics from all three hosts and accepted trace batches.
The full replacement healthy-collector diagnostic passed: added CPU 0.8600% / 0.8601% / 0.9122%,
added RSS 2.97 / 2.18 / 2.15 MiB (Core/A/B), zero swap, actual three-host metrics and 246 trace
batches. Original session 6493 exited 0 including cleanup. Evidence and its separate exit receipt
are under `data/qualification/020/20e4390d/resources-healthy-diagnostic-*`.
The corrected sequential lean trial passed: added CPU 0.8106% / 0.7939% / 0.8087%,
added RSS 2.895 / 2.168 / 2.125 MiB, zero swap. Every host's actual sampled baseline
and armed windows exceeds 900 seconds. The first lean trial was retained but not qualified
because its baseline was approximately 5 ms short; the runner now measures each host's
sample interval. `resources-healthy-sequence.json` records both profiles qualified.
Focused regression: 113 passed, no skips. Generated OpenAPI freshness passes.
Hosted CI 38772733967 passed all 35 validation jobs; publish was skipped.

Historical attempts below preceded the completed matrix recorded above.
Original Core project `35c13da42b` and intermediate project `c2ab20bd90` were retired with
volumes retained. Project `d8d299ba40` accepted Core job `01M50JJRH6HC7CWYGGEH423NRE`,
then correctly refused drain because the fixture omitted the file worker. Normal admin
cancellation reached `cancelled_unchanged` without activating a candidate. Its cleanup was
initially blocked by the native fixture's training lifecycle callback pointing at production.
Both idle Source node processes were identity-verified and restarted with the same immutable
runtime and the isolated callback `http://coire-core.lab:19470`; no journal or hold was cleared.
The normal scheduler reconciled terminal cleanup, and all three targets returned ready.
Private before-retirement evidence is `d8d299ba40/results/terminal-cleanup-targets.json`.
Retirement completed with all journals and volumes retained. Startup attempts 6 and 7
were retired with no accepted ownership or native children; ops required a pinned model ID.
Attempt 8 was also retired before native startup; the ops model setting requires its registry
UUID, and the API service enablement must match the full profiles. Attempt 9 (`coire-020-application-52d526f866`) reached staged, then safely failed unchanged
because a fresh Core generation 1 conflicted with a prior released native generation 1.
No candidate activated. The correction gives every Core test cohort a separate native
`state-<trial>` directory, preserving prior released journals. Identity-verified Source/guard
restarts restored normal terminal cleanup; all targets returned ready. That cohort is retired. Attempt 10 (`coire-020-application-304b0b9db7`, session 26139) passed staging and activated
a healthy replacement API. Its real tiny engines loaded, but both candidate and restored
Source smoke failed because gateway proxy headers required a serving election lease during
maintenance. Gateway resolution also hard-coded production port 9400. The original budgets
were not renewed. Job `01M50N4E9MME5TZE3WJT96XMVV` is now `recovery_required` with
`finished_at=null`; runner stopped and Source Core, independent executor, native agents/guards,
private frontends and ownership holds are retained. Authenticated engine inventories show all
nine edge-a smoke engines stopped and edge-b empty. DO NOT clear holds, retire this cohort as
successfully reconciled, or reuse its native state as an idle fixture.

Corrections in `gateway/proxy.py`, `gateway/resolution.py` and
`upgrades/gateway_smoke_scope.py` permit only the exact task-local executor serving scope
(with existing SQL lease admission and independent node validation) through maintenance and
use the configured node port. Public/expired/wrong engine/node/port/loading/actor contexts
remain fenced. Relevant checks: 76 scope/proxy/native tests, 6 resolution tests, and 61
lifecycle/cleanup/contracts/execution checks passed (the resolution suite overlaps these runs).
Strict mypy, Ruff and formatting pass. `coire-api:020-smoke-fix` built; policy/scan/SBOM outputs
are `data/qualification/020/20e4390d/api-smoke-fix-*`. This corrected image is NOT activated in
the retained immutable Source. Next work must use the documented explicit recovery/support
workflow and positively reconcile that owner, then repeat the application/native matrix with
a qualified corrected Source; never inject new code into a sealed running Source.
The current private result is `results/current-recovery-required.json`; main log is
`data/qualification/020/20e4390d/application-upgrade-attempt10.log`.

`validation/application_upgrade.py` now enables all six managed services via profiles
`upgrades,chat-files,mcp,ops`, and passes the isolated training callback to both nodes.
The added file-worker, ops and MCP images passed image policy, CRITICAL scan and SPDX SBOM.
The runner includes real installed native success/manual rollback/broken-candidate restoration;
none of those transitions has yet qualified. Positive degraded inference and physical/public
ingress remain separate open gates. Existing private roots and logs contain authoritative
operation and cleanup histories; do not reset them or reuse a node with outstanding holds.
The owner confirmed that contractors caused the outage and no remote power control exists.
Unobserved outage/restart is not physical failover timing evidence; controlled physical power
and cable cases remain open. Cloudflare is still deferred.

The owner asked for completed implementation/testing, then explicitly deferred Cloudflare until
last. Corrective code and automated/native single-engine qualification are saved on this branch.
This is **not** full release qualification; consult the latest section of [review.md](review.md).

Implemented: durable election safety and replay guards; protocol-v2 scoped/signed authority;
Core and native admission/lifetime fencing; exact immutable single/sharded residency with signed
rank/link proofs; bounded audit/acknowledgements; actor-bound one-host overrides; truthful outage UI;
bounded telemetry; and 019 maintenance/cohort recovery integration. Runbook, schema outputs,
CI process/collector/tiny-model gates, production images and locked native wheels are updated.

Evidence includes the integrated feature suite, full web suite, strict type/schema/migration
checks, independent process faults, actual OTLP/Promtool tests, all four image policy/scan/SBOMs,
and buffered/streaming tiny-model inference on edge-a with verified cleanup. Exact counts, local
artifact paths and immutable image IDs are in review.md. No weights ran on Core. No production
node service/configuration was replaced, no image published, and no merge performed.

Continue with these open gates, preserving their task status:

- T056/T057: provider-dependent Cloudflare configuration and public acceptance are owner-deferred.
- T065 is complete: 35 full-middleware/workflow/node-race/actual-SQL checks pass. Physical
  replacement remains a workflow-test seam; T073 retains the live armed upgrade/rollback matrix.
- T073: PP now passes with the admin-acquired, checksum-verified MIT GLM-4.7-Flash-4bit asset.
  TP and PP buffered/streaming generation and both owned-rank faults now pass with zero swap.
  All four mid-stream faults terminate in 0.027–0.333 s without false completion. The owner applied
  and then removed the approved en5 aliases; readback confirms cleanup. Never put commands inside
  question widgets; the owner needs normal copyable code blocks.
- A new audited task key exists in Keychain as `coire-020-qualification-key`; no credential question
  is needed. Production `/api/v1/admin/upgrades/targets` currently returns 404, so old 019 fixture
  records do not establish a current armed upgrade deployment.
- The approved OrbStack force-stop/restart completed and restored Core health. It does not prove
  physical power loss. The owner confirms automatic restart after power restoration; `pmset -g`
  verifies `autorestart 1`. No remotely controlled outlet has been identified.
- Participant resource qualification now passes both full healthy-collector profiles; see the
  measured results above. Earlier stub-collector and short diagnostic failures remain historical
  evidence, and the first lean window was correctly rejected as 5 ms short. The replacement
  trials satisfy actual per-host sample durations, original CPU/RSS bounds and zero swap.
- Actual hardened frontend lifecycle passed again with the two-second inspection cadence:
  promotion 21.437 s, hand-back 48.627 s. Containers/networks were removed. Component native
  coexistence passed separately; it does not prove a request through the container.
- CI 38751556045 completed 34 jobs successfully but composed integration failed one existing
  instance-drain deadline (12.746 s against 10 s). Preserve that failure; do not loosen the bound.
  Changed crypto/election/controller suites pass 215 tests, zero skips; strict typing passes all
  1,304 source files. Hosted CI must run again on the final source.
- T036 closes only after required live evidence. Deployed frontend/coexistence, physical faults,
  and drained rollback remain required; public timing is deferred with Cloudflare.

The owner did not authorize a merge of 020. Unrelated preexisting edits to `.agents/skills/`,
`.claude/skills/`, `.specify/templates/tasks-template.md` and Spec Kit workflow files are preserved
and excluded from feature commits. No extension hooks were configured. Credentials and raw model/
image/test artifacts stay outside Git. Reuse passing evidence and rerun only affected checks.

Prior validated runtime source: `484b38619d03a8633dafbdb5885d5c34fe7ce248`.
Current election fixes are under fresh real-host and artifact qualification; see the latest review entries.
This includes the durable Core journal/commit-before-ack path and the direct PyJWT dependency
needed by standalone native wheels. The final image table is at the end of review.md.
CI on that source passed lint, installed Source/guard SIGKILL recovery, native engine and image
engine gates. Its new failover engine test exposed a test setup mismatch: the fake-engine harness
used 30 seconds while this test and the existing evaluation fixture allow 120 seconds for model
warm-up. The fixture now sets that same 120-second setup bound and includes redacted diagnostics;
all failover deadline assertions remain unchanged. The corrected test passed on edge-a with the
existing verified Qwen Coder 0.5B asset. Hosted CI confirmed it, plus 6,069 unit/contract and 245 web
tests. A subsequent existing extraction test exposed retained hard links from our new fixture;
native/operator qualification now uses independent model copies, preserving acquisition inode
identity. Owned links were removed on both Studios with source link count one verified. Sequential
failover and training/extraction/evaluation now pass on edge-a (2 tests, 51.45 s); hosted CI 38685425820 confirms this correction and all 35 validation jobs pass. Persistent private evidence is in
`data/qualification/020/484b3861/`, with a SHA-256 manifest. Runtime images still correspond to 484b3861.


Continuation after the owner directed completion: the failed Core operation cannot use
019's expired-recovery interface (it requires Studio kind). Its exact test processes were
positively suspended after authenticated complete native inventories and stopped engines;
Core SQL/holds, stopped containers and volumes remain retained, with recovery unresolved.
Suspended empty networks were archived and removed only after exact project labels and
zero endpoints were checked; this freed Docker subnets without changing address pools.
The associated old native prefix must never be reused (`application_node.py` now checks
all prior maintenance directories). The collector was also stopped and its configuration
retained. Private evidence: `304b0b9db7/results/suspension.json`, inventories and archived
network/collector configurations.

`validation/application_source.py` uses the ordinary locked installer and bootstrap producer
to create entirely independent native Sources. Both are installed under
`~/.coire/qualification/020/coire-020-application-source-4c951b7bd8` on their respective
Studios. Their private source map is `data/qualification/020/20e4390d/fresh-native-sources.json`.
The runner now requires `COIRE_020_NATIVE_SOURCES` and a digest in `COIRE_020_API_IMAGE`.
Attempt 11 failed before any service startup because Docker's subnet pool was exhausted;
its collector/empty network was positively cleaned. Attempt 12 is running as
`coire-020-application-bb59eadcfb`, session 91384, using corrected API image
`sha256:52229a13343ea2b8f4dd04be6dda5c6e0c37fde1a5afb9d695505055bd69fd27` as initial Source.
Main log: `data/qualification/020/20e4390d/application-upgrade-attempt12.log`.
All six Core services started healthy. Resolution tests now cover all three URL branches:
8 passed. The matrix is still running; do not count it qualified before its actual outcome.
