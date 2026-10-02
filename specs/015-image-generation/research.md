# Research: Image Generation

**Date**: 2026-09-30. Local baseline: `ff69d75`. Research inspected source/package metadata;
no dependency installation, model download, engine execution or Studio mutation was performed.

## R1 — Reuse the actual repository boundaries

**Decision**: Add scheduler code to `apps/coire-api/src/coire_scheduler/`, runtime helpers beside
`coire_node/engines.py`, and new strict contracts in coire-core. Preserve existing UUID registry,
variant, node and instance references; image job IDs use the existing ULID convention in
`coire_core/models/files.py`. Current Alembic head is `0022_stopped_usage_outcome`.

**Rationale**: Directory names in the original roadmap are conceptual; the real scheduler package
is colocated with API source but deployed separately. There is no image generation implementation,
asset-kind enum, image page, classifier or `coire-blobs` volume yet. `ReservationHolder.IMAGE`
already exists. `coire_scheduler/{runs,files}.py` demonstrates deterministic durable dispatch;
`coire_api/{run_executor,run_reconciler}.py` demonstrates observation/reconciliation.

**Alternatives**: A new scheduler package, database or object-store service duplicates existing
infrastructure. Migrating existing IDs to slugs/ULIDs would break established contracts.

## R2 — mflux version and model-specific capabilities

**Decision**: Pin Darwin-only `mflux==0.20.0` (MIT), published 2026-09-21, publishing commit
`ada53237b2ac865bc649ef1d06ac1b19b6983865`. Release wheel SHA-256:
`5d60a278d67289be0da2e4ba0e8dcf938dd3c676a01c9064f64e3fb10e30767d`.
Use the root `uv.lock` and existing hash-verified staged installation, not a new engines.lock.

**Rationale**: Relevant requirements fit the current locked MLX 0.32.2, Torch 2.14.0,
Transformers 5.16.1, Pillow 12.3.0 and HF Hub 1.29.0. This is metadata compatibility, not a solved
or executed environment. Implementation must solve/pin and smoke mflux plus mlx-lm/mlx-vlm.
Sources: [release/provenance](https://pypi.org/project/mflux/0.20.0/),
[dependency metadata](https://pypi.org/pypi/mflux/0.20.0/json),
[locked VLM dependency metadata](https://pypi.org/pypi/mlx-vlm/0.7.3/json).

| Required shape | Backend candidate and acceptance boundary |
| --- | --- |
| txt2img / img2img / LoRA | Resident `ZImageTurbo`; reject negative prompts and nonzero guidance, which Turbo does not use. |
| fill | `Flux1Fill` with validated base, image and mask; model licence reviewed separately. |
| control | `ZImageTurboControlnet` or `Flux1Controlnet`, initially Canny; plain Turbo does not acquire this capability by label alone. |
| upscale | `SeedVR2` as a separately acquired/reserved stage; backend resolution mapping must match the advertised factor/output bounds. |

Sources: [Z-Image](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/models/z_image/README.md),
[Flux](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/models/flux/README.md),
[SeedVR2](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/models/seedvr2/README.md).

**Alternatives**: One generic model shape silently ignores fields. CLI-per-job loses resident
objects/caches. ComfyUI or a hosted provider violates this feature's chosen generation boundary.
All high-level modes remain required; per-model support is explicit rather than universal.

## R3 — Offline dependency closure and acquisition

**Decision**: Admin acquisition captures immutable manifests for every base, tokenizer, text
encoder, LoRA, control/upscale model and classifier. Localize the complete artifact tree; deny
network at test time and fail missing-file preflight before generation. Worker receives no HF token.

**Rationale**: mflux loaders may download missing dependencies even with a local base model.
Z-Image control expects a local `controlnet/` tree; Flux uses `transformer_controlnet/` and its
constructor `controlnet_path` is not forwarded. Build validated local composite manifests rather
than trust a parameter name. Optional PiD and HED/pose/depth preprocessors remain disabled until
separately acquired, licensed and tested. Sources:
[Z-Image initialization](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/models/z_image/z_image_initializer.py),
[Flux initialization](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/models/flux/flux_initializer.py).

The pinned Union 2.1 control acquisition selects only
`Z-Image-Turbo-Fun-Controlnet-Union-2.1.safetensors` from the reviewed
[Alibaba-PAI repository](https://huggingface.co/alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union-2.1/tree/main).
That repository has no `config.json`; pinned mflux 0.20.0 uses its local Union
2.1 defaults when the verified checkpoint is linked under `controlnet/` beside
the verified base tree. The temporary composite is local to the validation
worker and removed after the native smoke.

**Alternatives**: Opportunistic runtime pulls violate Principle V. A filesystem path by itself
is not proof that a pipeline is offline. Text perplexity validation cannot validate image assets.

## R4 — Resident caches and cancellation

**Decision**: Add bounded instrumented cache hooks around the pinned runtime's encoder and
preprocessing seams; one ordered patched LoRA stack, rebuilt from clean weights when changed.
Use stage callbacks plus node-owned hard termination, retaining reservations until death.

**Rationale**: Z-Image re-encodes each prompt; Flux's prompt dictionary is unbounded. Cache keys
must include full immutable identities and all stage inputs, not just prompt text. Source:
[Z-Image implementation](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/models/z_image/variants/z_image.py),
[Flux encoder](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/models/flux/model/flux_text_encoder/prompt_encoder.py).

Callbacks exist before/in/after the denoising loop, but in-loop callbacks precede lazy evaluation;
synchronize before completed-step telemetry. They do not bound time spent in encoding/decoding,
so cooperative flags alone cannot meet five-second cancellation. Source:
[callback interface](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/callbacks/callback.py).

**Alternatives**: Reuse an unbounded upstream cache, report optimistic progress, or release memory
on a cancellation request; each would misrepresent actual work or permit overcommit.

## R5 — Private core publication and exact settings

**Decision**: API streams into private staging, verifies receipts/recipes, waits for Studio cleanup,
and atomically publishes the batch under a locked job version. Downloads require both identity and
an expiring owner grant. CPU file worker extracts one bounded recipe chunk before normalization.

**Rationale**: Existing chat normalization converts RGB and strips metadata. Existing MCP artifacts
are retained on Studios and cannot be reused unchanged. Upstream generated-image metadata rounds
LoRA/control scales and may expose local paths; write canonical Coire metadata instead. Compare
pixel digests because file timestamps/timing are not pixel content. Source:
[upstream metadata](https://github.com/mflux-community/mflux/blob/ada53237b2ac865bc649ef1d06ac1b19b6983865/src/mflux/utils/generated_image.py).

The user clarified: identical pixels only for matching inputs, model revisions, runtime and
hardware; otherwise restore settings and disclose non-exact reproduction. No old-runtime archive. Follow-up analysis found a 10 MiB input / 64 MiB output mismatch; the user approved a separate recipe-only 64 MiB PNG import path with <=64 KiB metadata extraction and no pixel decode. Generation inputs retain their original bounds.

**Alternatives**: Bearer-only URLs leak across identities; publishing images one at a time breaks
batch cancellation; loading source PNGs in API shares native parser risk with the gateway.

## R6 — Human authorization and independent image budgets

**Decision**: Use an image-specific owner guard, personal key scopes, live entitlement checks,
mandatory admission/refusal audits, current download authorization and separate image/disk quotas.
Reject all service/run credentials for this release. Keep explicit dependencies monotonic when
presets are overridden. Entitlement removal cancels uncompleted explicit jobs and denies new
explicit downloads; historical provenance remains stored and private.

**Rationale**: `coire_api/auth.py::require_scope` restricts only API_KEY principals; other
credential classes could slip through it. Existing personal keys have a user ID and current
entitlements. Existing `identity/limits.py` budgets tokens, which is not an image cost measure.
Existing `identity/entitlements.py` supplies audited grants/revocations; reuse it.

**Alternatives**: Giving run tokens their owner's explicit privilege violates the explicit-human
boundary. Requiring a browser for every request would defeat the compatible endpoint.

## R7 — Output tagging stays on the Studio

**Decision**: Use direct existing Transformers/PyTorch on Studio CPU with
`Falconsai/nsfw_image_detection` revision `96cb0d0342c7afb80cab76ecc58b265fa44da256` (Apache-2.0).
Admin acquisition permits only safetensors/config/preprocessor/model-card files. The weight is
343,223,968 bytes with SHA-256 `97b2ce64ec146884b37f98ee7944ca4891aa72f6827dc0cb10684a1cbecd5830`.
Use `local_files_only=True`, `trust_remote_code=False`, `use_safetensors=True`.

**Rationale**: It exposes normal/nsfw classification with 224-pixel ViT input and requires no new
inference-wrapper service. Its proprietary training-data provenance and classifier quality must
be evaluated locally; labels are gallery metadata, not an authorization oracle. A failure yields
unknown and excludes future sharing without blocking entitled generation. Threshold is 0.5 for
nsfw probability in the initial version, recorded with model/processor version. Sources:
[pinned model card/licence](https://huggingface.co/Falconsai/nsfw_image_detection/blob/96cb0d0342c7afb80cab76ecc58b265fa44da256/README.md),
[configuration](https://huggingface.co/Falconsai/nsfw_image_detection/blob/96cb0d0342c7afb80cab76ecc58b265fa44da256/config.json),
[artifact metadata](https://huggingface.co/api/models/Falconsai/nsfw_image_detection/tree/96cb0d0342c7afb80cab76ecc58b265fa44da256).

**Alternatives**: Core classifier inference violates Principle II. An unknown-as-normal fallback
is misleading. A new hosted tagging service leaks content and adds an unnecessary dependency.

## R8 — Chat contention requires measured admission

**Decision**: Add shared atomic accelerator admission beside `node_admission_lock` and request
leases, with exact-runtime coexistence profiles and an image-dispatch circuit breaker. Default
placement prefers B. Unmeasured combinations wait rather than assume concurrent safety.

**Rationale**: `gateway/proxy.py` has a process-local semaphore and persisted leases that protect
against eviction. Neither provides cross-process GPU admission. One image job can still contend
with a language decoder. A same-node benchmark must prove the stated latency while images make
progress. Profiles invalidate on model/runtime/bounds changes and unsafe thermal/latency signals.

**Alternatives**: A fixed memory reservation does not guarantee decode latency. A universal
exclusive lock would stall new chat behind a long image and fail the loaded-chat objective.

## R9 — Compatible endpoint is a bounded adapter

**Decision**: Support synchronous text-to-image using registry model IDs, prompt/count/size,
profile-defined quality and URL/base64 output. Use the same durable native job. After the bounded
wait return a recoverable timeout containing `coire_job_id`; idempotency permits safe inspection
and retry. Unsupported provider-specific options are field errors, never silently ignored.

**Rationale**: Standard image responses carry `created` and `data`; entries can carry `url` or
`b64_json`. Coire retains authenticated URLs, so base64 is the portable option when a client
cannot add download credentials. Source:
[OpenAI image generation reference](https://developers.openai.com/api/reference/resources/images/methods/generate).

**Alternatives**: Returning a queue receipt with HTTP 202 from the compatible endpoint changes
its success shape. Holding HTTP indefinitely is fragile; a separate worker implementation would
duplicate authorization and results.

## R10 — Local tiny engine evidence and reviewable delivery

**Decision**: Create a deterministic test-only tiny Z-Image component fixture, assert total files
<=1 GB, and exercise the real inherited generation path through the node worker. Keep production
selection unable to activate the test factory. Separately verify actual full-size model families
on the cluster under operator control. All model files remain ignored, outside git.

**Rationale**: No tiny image fixture exists in this repository. A source-supported approach uses
real Z-Image transformer width 128, one layer/refiner/head and axes [32,48,48], a two-layer width-128
text encoder with vocabulary 256, the real default 16-channel VAE and a local tokenizer. Generate
deterministic random safetensors at test setup; use the real encoder, denoiser and decoder. This
has not been executed or measured; fixture feasibility and the size assertion are explicit tasks.
Fake-worker integration proves orchestration but cannot substitute for this engine gate.

**Alternatives**: Claiming a production Turbo model fits <=1 GB is unsupported. Downloading a
full model in CI violates the tiny-model gate. Splitting this roadmap into reviewable child specs
honors CONTRIBUTING; the parent retains end-to-end coverage and does not declare success after MVP.
