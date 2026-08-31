# Quickstart: Validate the OpenAI/Anthropic gateway

## 1. Contract and regression gates

```bash
uvx --from openapi-spec-validator openapi-spec-validator \
  specs/003-gateway-openai-v1/contracts/openapi.yaml
uv run pytest -q packages/coire-core apps/coire-api/tests/unit apps/coire-api/tests/contract
uv run mypy apps/ packages/
uv run coire-api export-openapi --check
```

Expected: schemas forbid unsafe extras at internal boundaries, public compatibility fixtures parse,
unknown/unpublished/unentitled ids are indistinguishable to users, and engine-bound payload tests
prove the caller's `model` value is replaced by the registry-resolved engine value.

## 2. Integration topology

```bash
COIRE_INTEGRATION=1 uv run pytest -q -m integration tests/integration/test_gateway.py
```

Expected: the official OpenAI SDK lists and chats in streaming/non-streaming modes; the official
Anthropic SDK streams by changing its base URL; a cold request waits with keep-alives; no-wait and
saturation return bounded retry responses; disconnect/failure each create one usage row.

## 3. Real tiny-model gate

Acquire and load the existing ≤1 GB test model through the admin API, publish it for the test
principal, then run the gateway benchmark with `COIRE_TEST_MODEL` set to its registry UUID.

Record:

- 20 loaded-model first-token observations (p50/p95/max; p95 ≤1.5 s);
- gateway-only overhead from paired direct-engine/gateway probes (p95 ≤20 ms);
- cold-load duration and keep-alive interval;
- one completed, one disconnected, and one engine-failed usage row;
- engine/node/model/request identifiers in the corresponding trace/log chain.

## 4. Safety and rollback

Verify an arbitrary string, repository id, slug, adapter suffix, unpublished UUID, and unentitled UUID
never contact the engine. Roll back the API image and migration; confirm registry/engine rows remain
unchanged and the old `/api/v1/models` route still works.

Do not mark the real-model task complete on a skip.

### Recorded real-cluster result — 2026-08-31

Topology: `coire-core.lab` gateway container over Wi-Fi to the bare `mlx_lm.server` on
`coire-edge-a.lab:9501`, using registry model `336ec191-15a5-4c90-8526-5c9712303d5b`
(`mlx-community/Qwen2.5-0.5B-Instruct-4bit`, 289 MB). The loaded engine reported a 5.49 s cold-load
duration; the configured gateway loading keep-alive interval was 5 s.

Twenty one-token SSE requests were made after one warm-up and consumed through `[DONE]`. External
request-to-first-token was p50 504.91 ms, p95 514.28 ms, max 612.27 ms (SC-004: pass). The gateway
recorded the exact request-to-upstream-first-byte interval for the same requests and subtracted the
engine interval: gateway-only overhead was p50 5.80 ms, p95 7.51 ms, max 8.17 ms (SC-003: pass).

As a diagnostic cross-check, direct-engine and gateway probes were also alternated and bracketed.
Their raw subtraction was dominated by the model/Wi-Fi variance (roughly 400–700 ms), confirming
that the in-request OTel measurement is required to evaluate a 20 ms gateway-only threshold.
