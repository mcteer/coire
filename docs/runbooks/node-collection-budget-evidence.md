# Native node collection budget qualification — issue 93

Follow-up to [feature 017](../../specs/017-evaluation-verbs/spec.md) and [PR 92](https://github.com/mcteer/coire/pull/92), addressing [issue 93](https://github.com/mcteer/coire/issues/93) under the bug-fix workflow. Requirements remain [009 FR-006a / SC-014](../../specs/009-observability-stack/spec.md). Operational procedure: [node collection budget](sharded-serving.md#node-collection-budget).

## Qualified runtime

On 2026-10-09 UTC, both real Studios ran `/opt/coire/envs/0.2.0-a68cf8319860` with diagnostics disabled and unchanged limits of **2% CPU and 157,286,400 bytes (150 MiB) RSS**. Protected launchd configuration was unchanged; no private profiling module was installed. Core evaluation/training admission remained disabled. Existing admin-acquired, registry-verified Qwen2.5-Coder-0.5B assets were used through authenticated gateway/node paths; coire-node owned the engines.

Wheel SHA-256:

- Core: `cbe1ec7a9141cb49168aaeb958b9c2ca0f54866853db6c593cc4e68bf443f2f1`.
- Node: `905d61436e2cb058a953b73d4b26ca9da5c74dd608386226ee82161a9067fb32`.

## Measurements

Each node had **108 authenticated health polls and 53 distinct collection snapshots**, with no rejected or filtered failure samples. Collection began after 123 seconds of uptime. Studio A covered 01:35:54.971037–01:44:48.720820 UTC; Studio B covered 01:35:55.959412–01:44:49.029045 UTC. Every poll reported `collection_budget_ok=true`, zero swap and nominal thermal state.

| Phase | Distinct samples per node | A maximum CPU / RSS | B maximum CPU / RSS |
| --- | --- | --- | --- |
| Before generation, including preparation | 17 | 1.5% / 142.96875 MiB | 1.5% / 144.59375 MiB |
| Sustained generation | 18 | 1.1% / 144.578125 MiB | 1.1% / 146.21875 MiB |
| After both engines stopped | 17 | 1.5% / 144.671875 MiB | 1.4% / 146.296875 MiB |
| Entire observed window, including transition | 53 | **1.5% / 144.671875 MiB** | **1.5% / 146.296875 MiB** |

Generation ran 01:38:45.381520–01:41:47.800505 UTC: **106 successful requests, 53 per node**, with two concurrent node loops for at least 180 seconds. Both authenticated inventories subsequently showed no running containers and only STOPPED engine records. Post-drain collection continued for at least 171 seconds. Six fresh real capability inspections (TP/PP/TP on each node) returned HTTP 200 with expected support results true/false/true.

This qualifies the observed steady-state window, not startup or arbitrary future workloads. Activation receipts retain first-second CPU samples above budget; no startup passing claim is made. Earlier candidates failed CPU or post-generation RSS limits and remain unqualified. A private profiling experiment stalled Studio B and was removed before this final normal-runtime qualification; authenticated drain checks and runtime attestations passed after restoration. No budget, watchdog, ownership, authentication, TLS, or network gate was relaxed.

## Validation and evidence

Local verification: **2,852 CPU unit/contract tests passed**, 43 existing opt-in skips and 501 integration cases deselected; strict mypy passed across 927 files; Ruff formatting/checks and OpenAPI freshness passed. Focused public-model export tests passed after the final test-only formatting correction. Promtool validated sustained budget failure, healthy-node isolation and recovery. CI production-image and isolated-integration results are tracked on the fix PR.

Raw credentials, health samples, engine inventories and generated outputs remain outside Git under `/private/tmp/coire-node-budget-20261008`. Qualified evidence references:

- `qualification-clock-lazy-192.168.4.{11,12}.jsonl`, `qualification-summary.json`.
- `inference-clock-lazy.log`, `inference-clock-lazy-outcomes.json`.
- `probes-clock-lazy-192.168.4.{11,12}.jsonl`.
- `drain-clock-lazy-final.jsonl`, `runtime-attest-192.168.4.{11,12}.json`.
- `qualified-evidence-sha256.json`, `cpu-clock-lazy.log`, `mypy-final.log`, `openapi-clock-lazy.log`, `alert-final.log`.

Previous immutable native environments remain available for the documented drained binary rollback. The additive journal index preserves history and compatibility with older binaries.
