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

- T056/T057: provider-supported Cloudflare monitors/pools/fallback and public acceptance are
  owner-deferred. `deploy/cloudflared/load-balancer.yaml.tmpl` is explicitly a design manifest,
  not a deployable provider API payload.
- T065: full armed-020 upgrade-success and failed-candidate rollback matrix remains unqualified.
  Full API middleware fences and signed exchanges, actual node streaming/start admission,
  cohort-readiness and SQL ownership checks pass; do not present those as a full live upgrade.
- T073: live TP failed because RDMA en5 lacks IPv4 on both Studios (fabric IPv4 is on bridge0).
  An async owner question is pending for temporary `169.254.200.11/16` and `.12/16` en5 aliases,
  with cleanup after the isolated test. **No approval was received and no aliases were added.**
  AGENTS.md requires permission for network changes. The operator-only reproducible runner is
  `validation/native_cluster.py`; copy it and the latest native/core source to both isolated
  `/tmp/coire-020-qualification` checkouts before rerunning. It preflights native capability.
- PP also needs an acquired/verified pipeline-capable asset: the available tiny Qwen and SmolLM
  assets do not implement PP in the pinned MLX-LM. Do not silently substitute or acquire one.
- Real Core power loss needs a verified independent recovery path. Core is the agent host.
  Fifteen-minute armed resource deltas, Studio swap/coexistence, deployed rollback and public
  60/90-second timings remain unmeasured. The 79 MiB frontend sample is only a short container test.
- T036 closes only after required live evidence. Never convert these open gates to checked tasks
  on the strength of the passing unit/process tests.

The owner did not authorize a merge of 020. Unrelated preexisting edits to `.agents/skills/`,
`.claude/skills/`, `.specify/templates/tasks-template.md` and Spec Kit workflow files are preserved
and excluded from feature commits. No extension hooks were configured. Credentials and raw model/
image/test artifacts stay outside Git. Reuse passing evidence and rerun only affected checks.

Implemented runtime source commit: `817ff11b2beb74c0c3f2d6a227d24fb5b2761302`.
