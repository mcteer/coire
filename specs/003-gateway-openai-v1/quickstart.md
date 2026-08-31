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
