# Implementation handoff: 020 control-plane failover

Updated 2026-10-15 on `feat/020-control-plane-failover`, based on merged 019 (`cd355554`).
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
- Participant CPU qualification remains open. Full trials with the stub collector and actual Docker
  lifecycle failed (lean 1.502%/1.347%, diagnostic 1.594%/1.486%). The fixture now runs the real
  native metrics collector in both baseline/armed windows and reads kernel swap without forking.
  A short diagnostic still exceeded CPU. The controller now inspects on the existing two-second
  bound; election participants reuse member-owned parsed signing keys and immutable peer URLs.
  No signature/authority result is cached. The latest short diagnostic is
  `/tmp/coire-020-cpu-prepared-runtime.log`; full replacement windows are still required.
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
