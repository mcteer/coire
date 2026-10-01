# Quickstart and Acceptance: Image Generation

This guide describes validation **after implementation**. The planning pass did not execute
these checks or operate the Studios. A fake worker proves orchestration, not native image quality
or chat coexistence. Do not mark a required gate complete with skipped tests.

## Prerequisites

Use the implementation child branch and frozen workspace, Python3.13 and the repository's Node
version. Local integration uses the isolated `coire-it` project from
`tests/integration/conftest.py` with simulated nodes, generated credentials and scratch volumes.
Never target production Compose or real Studios from CI. Real tiny-engine tests require a local
Apple Silicon development Mac that is not core. Only an operator performs the real-cluster matrix
through the existing audited admin/node APIs; no direct engine/SSH/Docker commands on Studios.

Before a manual UI test, an admin must acquire, validate, replicate and publish the test image
models and compatible auxiliary assets. Use existing identity APIs to create an ordinary user,
an explicit-entitled user and personal keys with the relevant scopes. Do not put credentials in
this document, shell history, test fixtures or checked-in environment files. Configure quotas and
model IDs through documented environment/admin settings. [Contracts](contracts/images.md) define
request shapes; [data-model.md](data-model.md) defines state and ownership.

## Local static, unit, contract and web gates

```sh
uv sync --all-packages --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps/ packages/
uv run pytest -q -m 'not integration and not engine'
uv run python -m coire_api.openapi --check
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
pnpm -C apps/coire-web build
```

When implementing schema changes, regenerate rather than edit generated files:

```sh
uv run python -m coire_api.openapi
pnpm -C apps/coire-web exec openapi-typescript ../coire-api/openapi.json -o src/api/schema.d.ts
```

Review the generated diff and rerun freshness/contract checks in the same child commit. CI uses
npm for the web build; if package metadata changes, keep npm/pnpm lockfiles consistent. No new UI
library is planned. Run migration upgrade/downgrade tests against scratch Postgres and existing
text/VLM rows, with one reversible migration per child PR.

## Local simulated-node integration

The following proposed tests are created by tasks.md; these commands are not claims that the
files or results already exist:

```sh
COIRE_INTEGRATION=1 uv run pytest -q -m integration tests/integration/test_015_image_jobs.py tests/integration/test_image_recovery.py tests/integration/test_image_isolation.py
```

Prove receipt latency; ordered queued/started/progress/done events; same-key deduplication; key
reuse conflict; owner scoping; expired grants and refresh; per-user/global/disk budgets; both-node
replication eligibility; no image assets in chat/MCP/failover resolution; unknown classifier
behavior; and no implicit acquisition. Capture counters before/after to prove holds release once.

Interrupt scheduler during each durable phase, API during upload/publication, and simulated node
during generation. Reattach to the same journal/receipt, or fail visibly without regeneration.
Race cancel against every transfer/publication boundary. A partition must leave the job cancelling
and reservations held until termination is proven; no user download succeeds. Verify one terminal
result, no partial batch, no repeat quota charge, and eventual core orphan removal. Exercise node
cleanup acknowledgment loss separately from actual failed cleanup.

## Real local tiny-engine gate

Create a **test-only** fixture builder at
`apps/coire-node/tests/engine/build_tiny_image_fixture.py` using the real mflux components described
in research. It writes deterministic random safetensors/tokenizer files to ignored storage, records
its manifest and fails if total bytes exceed1GB. The production worker cannot select this factory.
No automatic Hub download is permitted. The small fixture demonstrates mechanics, not image quality.

After that builder and fixture are implemented:

```sh
COIRE_TEST_MODEL="$PWD/models/test--image-tiny" uv run python apps/coire-node/tests/engine/build_tiny_image_fixture.py
COIRE_ENGINE=1 COIRE_TEST_MODEL="$PWD/models/test--image-tiny" uv run pytest -q -m engine apps/coire-node/tests/engine/test_real_image_worker.py
```

Exercise the real worker/encoder/MLX denoiser/VAE decoder, progress synchronization, same-pixel
round trips, prompt cache, hard cancellation, PID re-adoption, output transfer and scratch cleanup.
Run with outbound model-fetch paths denied; a missing component must fail before engine execution.
Record hardware/OS/runtime, fixture manifest/size and passed test names in the execution record.
If this fixture cannot execute or fit, keep the gate incomplete and fix it; fake worker results
cannot substitute. Recheck existing text/VLM engine smoke after installing the combined lock.

## User journeys

| Scenario | Expected result |
| --- | --- |
| Private UI generation | Choose eligible model/preset; form reflects capabilities; queue receipt <=1s p95; events visible <=2s; whole batch appears after cleanup; authenticated PNG download. |
| Compatible client | Use personal key with registry model UUID; request b64_json and URL separately; standard response shape; timeout includes recoverable job ID and key replay deduplicates. |
| Cancellation | Cancel queued, running, encoding, decoding and transferring work; healthy execution ends <=5s; no downloadable output for a cancelled batch. |
| Explicit policy | Entitled human/key succeeds and audits; non-entitled direct dependency/preset and all service/run credentials fail; revocation during queue/run/transfer prevents publication. |
| Historical access | Expired grant refuses; owner refresh succeeds; cross-owner and revoked explicit access refuse; deletion denies immediately and eventually frees disk quota. |
| Recipe | Drag own PNG back, including valid generated PNGs above 10 MiB and at the 64 MiB cap; reject over 64 MiB recipes and over 10 MiB generation inputs, and assert recipe extraction never decodes pixels; all effective fields restore once, without doubling prefix; ten same-environment repetitions match pixel digests; missing inputs/changed versions are named. |
| Advanced modes | For compatible model families exercise init/strength, mask, Canny control, multiple ordered LoRAs and upscale; wrong kinds/unsupported settings reject before worker allocation. |
| Cache iteration | Twenty warm-cache runs changing seed/steps avoid re-encoding; changed prompt/encoder/adapter causes miss; bounded eviction and worker reload miss honestly. |
| Gallery | Filter normal/explicit/unknown; reuse settings and new-seed generation; all images private; unknown tagging gives a diagnostic without blocking entitled owner. |
| Accessibility | Keyboard-only complete journey, labelled fields, focus after error/cancel, screen-reader progress, light/dark and 1024/1440px layouts. |

## Required operator cluster acceptance

Record results in `specs/015-image-generation/execution-record.md` during implementation.
Use non-sensitive test prompts and only admin-acquired model assets. Do not commit generated PNGs.

1. Record both Studio hardware/OS/runtime fingerprints, complete component manifests, licences,
   registry IDs and baseline memory. Verify no core model package/process/weights were introduced.
2. Through admin acquisition validate each production model family: txt2img/img2img/LoRA,
   fill, Canny control and upscale. Prove both copies verify and runtime missing-file failures
   never trigger downloads. Classifier stays on Studio CPU with measured memory and recorded labels.
3. Generate/transfer a batch, inspect durable core receipts and verify Studio input/output scratch
   is empty **before** success. Inspect restart cleanup after a failed/cancelled attempt.
4. Exercise node re-adoption, worker crash, cancel/TERM/KILL, idle 15 min unload and pin override;
   memory release follows actual process death. Confirm one resident worker and one active image
   executor per Studio; default preference B and explicit pinning are honored.
5. Prove 20 warm-cache seed/step trials and ten exact-environment pixel round trips. Inspect spans
   with diagnostics enabled and metric counters with diagnostics disabled. Changed LoRA stack
   uses one clean replacement, not accumulating patched weights.
6. Benchmark 15 minutes of chat (<=4k prompts) plus repeated image jobs on the **same node**.
   Record warm baseline, mixed first-token p50/p95, decode throughput, actual image completion,
   reservations, measured footprint and thermal state. Pass requires chat first-token p95<=1.5s,
   gateway overhead<=20ms excluding model work, no swap and image progress. Store the approved
   coexistence profile. An unmeasured or failing pair must refuse/wait for image dispatch; do not
   weaken the gate or describe serialization alone as successful protection.
7. Test memory/storage pressure, unknown tagging, stalled queue and cancel/cleanup failures;
   verify dashboard panels and actionable alerts. Lean mode still has audit/metrics/alerts and
   reports unavailable trace history. Re-enable diagnostics and confirm attribution.
8. Test drain-before-rollback, expired transfers, paired DB/blob backup restore and orphan cleanup
   according to the runbook; no old worker may publish after its job is fenced or tombstoned.

## Release evidence

Record command outcomes, runtime fingerprints, migration result, contract/OpenAPI/TS freshness,
web/browser checks, node installer smoke, local engine results, cluster matrix and limitations.
Build/scan affected images, verify SBOM/no-shell policy and `docker compose config` locally/CI;
retain existing network, capability and secret policies. Confirm new observability files are
included by the actual Prometheus/Grafana deployment configuration. PRs link their child spec and
parent 015, cite Principles I–VII (including II-a), and explain the mflux MIT dependency plus model
licence reviews. Required security/deploy changes receive the repository's two-reviewer gate.
