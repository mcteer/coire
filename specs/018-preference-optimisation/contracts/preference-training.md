# Preference Training and Node Contracts

All wire shapes are strict coire-core models. This design extends existing dataset/admin training/node boundaries; public `/v1` behavior stays compatible. No implementation or runtime capability is claimed by these documents.

## Version 3 intent

Add `TrainingSpecV3` beside unchanged v1/v2 SFT documents. Extend `TrainingSpecDocument` parsing by schema_version, preserving the absent-version default of 1. V3 has the existing model/data/parameterization/optim/output/placement/seed structure with these additions/restrictions:

| Field | Contract |
|---|---|
| `schema_version` | Exactly 3 |
| `objective` | `dpo` or `orpo`; SFT remains v1/v2 |
| `objective_options` | DPO: `{beta: number}` finite 0 < beta ≤ 1; ORPO: `{weight: number}` finite 0 ≤ weight ≤ 1. Require the shape matching objective, no extra knobs. Recipe default 0.1 for either coefficient. |
| `init_adapter` | Registered adapter UUID or explicit null for bare start; never a path or Hub ID |
| `data` | Existing immutable mixture references, all preference format, `loss_policy=final_assistant`; train/validation prompt groups disjoint across all sources |
| `parameterization` | Dense `lora` or acquired uniform affine 4-bit/group-64 `qlora`; approved llama/qwen2 architecture/targets; dropout exactly 0; old rank/scale/layer bounds retained |
| `optim.batch_size` | Number of pairs, 1–64; each pair includes two complete sequences; complete equal-size batches and existing accumulation/update bounds |
| `optim.max_sequence_length` | Existing 2–8192 bound on each prompt-plus-answer; actual measured profile may support a narrower bound |
| `placement` | `mode=single`, optional declared Studio; `data_parallel` refused |
| `eval` | Existing held-out objective cadence/end flags; `suites=[]` or up to four feature 017 schedules, ≤32 valid pre-final committed checkpoint updates |

Recipe fragments (not full runnable recipes):

```yaml
schema_version: 3
objective: dpo
objective_options: {beta: 0.1}
init_adapter: null
# model/data/parameterization/optim/output/placement/seed remain required as above
```

ORPO uses `objective: orpo` and `objective_options: {weight: 0.1}`. Full admin templates in `recipes/training/{dpo,orpo}.yaml` must be bound to real registry identities by the UI/CLI. Original YAML, source hash and normalized v3 intent hash are stored independently. Frozen v1/v2 fixtures prove identical parse/dump/hash and resume behavior. Preference input never coerces into SFT, and format mismatch is a typed preflight refusal.

## Existing API extensions

Existing `/api/v1/admin/training/validate`, `/recipes`, `/jobs`, job detail/history/control/events and measurement routes accept/expose v3 documents. Preserve current idempotency, active admin/key, origin, audit and scope rules. Add typed objective diagnostics and v3 resource/profile identities to validation and measurement results. `TrainingMeasurementRequest.spec` must accept all supported document versions explicitly; native measurement dispatch must run the requested objective. `COIRE_PREFERENCE_TRAINING_ENABLED=false` refuses new v3 jobs/measurements while normal history/cancel/cleanup remain available. Controlled admin qualification when enabled follows existing guard/admission rules and may establish the first objective profile.

Add `GET /api/v1/admin/adapters/{adapter_id}/lineage` returning bounded `AdapterLineage` with immutable manifests, objective/dataset/source job and ancestors (≤32). Existing adapter detail objective expands to dpo/orpo. Parent selection requires ready exact compatible artifacts and pins them; it does not require parent write verification because training is an admin action. Results always start admin-only/unverified. Retirement refuses while active/paused job pins require artifacts; old metadata remains readable after allowed retirement. Child serving does not open ancestor artifact paths.

Existing `coire train` YAML/form/validate/measurement and adapter commands must support v3 and lineage via API. Admin UI shows objective, coefficient, initialization target, approved single-node parameterization, dataset warnings, exact lineage and durable objective/probe metrics. Task/judge evaluation scores remain separate from training loss and harness verification.

## Canonical preference examples and rendering

Input JSONL row: `{"prompt":[{"role":"user","content":"..."}],"chosen":"...","rejected":"..."}`. Prompt supports an optional first system message followed by alternating user/assistant text messages ending user. No tools/attachments/images/hidden reasoning/code actions, unknown keys or caller executable content. Core canonicalizes semantic role/content values without tokenization and computes separate prompt-group and full-row hashes; original bytes remain immutable.

Studio uses the same bare chat-template/rendering primitives as serving. Render both complete prompt/response sequences and verify identical prompt/generation prefix; shifted masks select only answer and response-termination tokens. No extra generation suffix after a completed answer. Reject empty/identical visible answers, token-identical answers, zero-target masks, non-prefix-compatible templates or either side overlength; do not truncate one side into a different preference. Preference token caches are keyed by source/split/template/tokenizer/runtime/renderer identity and purged through existing leases/retention. No tokenization on core.

## Numerical objective contract

For each complete response, let `s` be the sum of shifted supervised token log-probabilities and `n > 0` the supervised count. Compute log-softmax and reductions in FP32. Mask padding/prompt by selection, not multiplication of possibly non-finite losses by zero. Gradients flow only through trainable policy adapter tensors.

DPO: `z = beta * ((s_policy_chosen - s_policy_rejected) - (s_reference_chosen - s_reference_rejected))`; objective is `mean(softplus(-z))` over pairs. Reference probabilities are detached; reference is eval-mode, fully frozen, matching base plus initial adapter (or bare base). Post-update accuracy is mean `z > 0` (ties false); margin is mean z. Sequence sums are intentional. Source: [DPO authors' implementation](https://github.com/eric-mitchell/direct-preference-optimization/blob/main/trainers.py).

ORPO: `a = s/n`; `odds(a) = a - log(1-exp(a))`; objective is `mean(-a_chosen + weight * softplus(-(odds(a_chosen)-odds(a_rejected))))`. Use per-example chosen NLL then pair mean, not a globally token-weighted NLL. Evaluate log1mexp with stable piecewise log1p/expm1; clamp only the odds calculation input to at most `-1e-7` in FP32 to avoid the zero endpoint. Weight zero skips odds computation and equals chosen NLL. Post-update accuracy compares chosen/rejected odds, ties false; margin is their difference. Source: [ORPO equations 3–7](https://arxiv.org/html/2403.07691v2).

Use unchanged `mlx_lm.tuner.trainer.train/evaluate` custom loss/iterator hooks. Return pair count as the aggregation count. Equal complete microbatches and accumulation produce the same declared pair mean. Count response tokens separately for metrics; upstream internal token counters cannot be exposed as real tokens for these losses. No Python side effects inside a compiled loss to capture auxiliary metrics.

Post-update metrics probe the first min(8, validation pair count) canonical held-out pairs at loss-report boundaries using eval mode, restoring policy mode/RNG/sampler afterwards. Record probe scope, update and count distinctly from aggregate pre-update training loss. Include probe overhead in measured resource/coexistence profiles. Validate all metrics finite; numerical fault stops through normal guarded lifecycle, with no false successful checkpoint.

## Resolved inputs, capabilities and transport

`ResolvedTrainingSpecV3` and companion value types freeze objective/options/implementation version, initial/reference manifests, preference split/analysis/template/tokenizer identities, pair-sampler version, full resource envelope, worker/runtime and optional resolved suite obligations. Every existing embedding must parse v3 deliberately: submission/validation/job detail/measurement, scheduler durable commands, leases/prepare/status, dataset grants/imports, checkpoint requests/acks, full-state manifests, extraction, evaluation training inputs and retention links.

Advertise support as schema version 3 plus supported objective implementation and measured objective/parameterization matrix in node training capability. Old nodes reject before payload dispatch. Existing v1/v2 capabilities remain truthful. Add `CheckpointCommitAcknowledgementV3` preserving job/attempt/fence/update and optional feature 017 evaluation pause; supervisor version validation must accept precisely the negotiated contract. Do not simply return a v2 acknowledgement to a v3 worker.

Dataset analysis may occur before training profile qualification, but remains bounded node-owned token analysis with existing resource limits. V3 measurement uses actual preference loading/paired batches/reference/probes/checkpoints; profile key includes objective/options, exact initial/reference target, objective-data/implementation version, native package versions, hardware, parameterization, batch/accumulation/sequence and resident chat set. Never use SFT config normalization or profile identity for v3. Reject stale/missing evidence before ordinary job launch. Existing controlled admin measurement establishes evidence under live safety guards.

## Full-state training, evaluation and retention

Fresh run: validate/pin exact local input copies; construct policy architecture/adapters; load initial adapter tensors if selected; construct independent DPO reference using original initial artifacts; initialize optimizer/sampler/RNG deterministically. Explicitly preserve policy RNG across any reference loader that reseeds globally.

Resume: validate all immutable digests; construct DPO reference from original initial target; restore checkpoint policy tensors, optimizer/schedule, sampler cursor/current RNG and completed update. Never load initial adapter after restoring trained policy. Checkpoints use `CheckpointWorkerStateV3` with objective-specific identities and pair-sampler state; no silent weights-only recovery. Both Studio copies independently verify before durable fenced commit. Stop/fence proof still controls reservation release.

Initialization manifest/config checks require matching base variant, architecture, parameterization/rank/scale/layer selection/target keys and tensor shapes/dtypes. Copy complete final adapter tensors into a standalone result, validate and mirror through existing extraction. Pin initialization artifacts through paused/recoverable job lifetime; no ancestor fusion or runtime stack. Parent metadata depth ≤32, no cycles.

V3 with nonempty suites resolves feature 017 contracts, authorization/grants and checkpoint/final obligations; v3 with empty suites does not create evaluation jobs. Declared final suites remain mandatory even if held-out objective `at_end=false`. Base comparison is the original bare base, not silently the parent adapter. Evaluation-owned checkpoint pauses use existing pin, owned stop/cleanup and fresh resume admission; later admin pause/cancel wins. Training success and evaluation failure remain independent. Only the exact new adapter's successful harness gate changes verified status.

## Compatibility, rollback and required proofs

Keep old recipe/resolved/checkpoint/profile identities and SFT numerical behavior. One additive migration with tested drained downgrade; old runtimes never receive v3. Disable v3 submissions, stop/drain v3 training/measurements/evaluation-owned work, exports and comparison generation, then roll back binaries while retaining additive schema/history. Disable privacy-producing controls too if old code cannot honor new generations; finish pending withdrawal cleanup before rollback or keep the upgraded cleanup owner running. Do not downgrade away retained v3 data silently.

Required numerical gates: independent FP64 oracle versus FP32 loss/logp `rtol=1e-5, atol=1e-5`, gradients `rtol=1e-4, atol=1e-5`; DPO initial equality gives log(2) with nonzero policy gradient; frozen reference/base tensors unchanged; ORPO zero-weight equals NLL; extreme logits finite; unequal lengths/mask/accumulation correct. Full process resume losses `rtol=1e-4, atol=1e-5`, adapter/optimizer tensors `rtol=1e-5, atol=1e-6`, exact sampler/RNG. Tolerance changes require evidence and a spec amendment.

Tiny-model CI proves both objectives × dense LoRA/affine QLoRA × bare/parent start, train/resume/exact serve, false inherited verification, v1/v2 regressions and v3 evaluation ownership. Real Studios qualify every advertised objective/parameterization, including chained starts, memory/probe/checkpoint peaks, five-second cancellation, restart, both-copy integrity, shared-chat latency/swap, privacy export and drained rollback. Tests refusing unsupported distributed/DoRA combinations are passing assertions, not skipped implementations.

## Internal measured gateway

`POST /api/v1/internal/training/measurements/{measurement_id}/generate` accepts
strict `TrainingMeasurementGenerateRequest`: `principal_sha256`, exact `target`,
frozen `prompt`, and bounded `max_output_tokens`. A node bearer credential and
`X-Coire-Node` are required. The node must belong to the running measurement.
The gateway reconstructs the principal from its persisted command and checks the
digest, then freshly validates live authority and exact measured workload before
using ordinary proxy leases, stream accounting and disconnect checks. Response:
`TrainingMeasurementCompletion`, with no generated content. Unknown ownership or
node binding is opaque 404; missing/bad credentials 401; ended owner authority
403; changed workload/residents/holds 409; extra fields 422. Scores/reports and
caller-supplied principals are never accepted.
