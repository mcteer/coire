# Adapter extraction evidence and integration handoff

Date: 2026-10-05. Branch: `feat/016-sft-training-jobs`.

## Owned implementation

- `apps/coire-node/src/coire_node/training/extraction.py`
- `apps/coire-node/src/coire_node/routes/training_extraction.py`
- `apps/coire-node/tests/contract/test_training_extraction.py`

`AdapterExtractor` validates immutable checkpoint identity, every checkpoint file digest,
all rank adapter/optimizer safetensors headers, rank-state identities, the resolved spec/runtime,
and the exact acquired base. Base lookup uses the frozen manifest digest inside `Store`;
requests cannot supply a path or import expression. Native compatibility is bounded to the
same approved dense llama/qwen2 linear targets as SFT preflight. It checks configured layer,
attention and MLP dimensions against base headers and adapter keys/shapes/dtypes. Uniform
affine 4-bit/group-64 QLoRA additionally requires packed weight, scale and bias headers.
The pinned bare constructors produce FP32 adapter tensors, including DoRA `.m` magnitudes.

Extraction uses header slices and bounded byte streaming, never `get_tensor`, a model loader,
MLX/Metal work, a trainer subprocess, or a modified trainer. The output contains exactly
`adapters.safetensors`, `adapter_config.json`, and `manifest.json`. Rank-zero adapter bytes are
copied unchanged. Native config contains `fine_tune_type`, `num_layers`, and
`lora_parameters` (`rank`, `scale`, `dropout`, explicit `keys`); QLoRA uses native type `lora`.
Additional `coire_` fields bind exact base model/variant/manifest and source checkpoint.
The manifest preserves job/attempt/fence/update/runtime/resolved-spec lineage.

Compatibility source inspected: upstream mlx-lm tag **v0.31.3**, files
`mlx_lm/tuner/{utils,lora,dora}.py`. `load_adapters()` consumes the three native config fields
above; `linear_to_lora_layers()` selects the last configured layers and explicit module keys.
Native linear shapes are A `[input, rank]`, B `[rank, output]`, and DoRA magnitude `[output]`.

## Required registration by the agent.py owner

Construct once, before the control app accepts requests:

```python
from coire_node.training.extraction import AdapterExtractor

extractor = AdapterExtractor(training_artifacts, reservations, settings, store)
extractor.attach(control_app)
```

Constructor inputs are the existing `TrainingArtifacts`, `ReservationLedger`, `Settings`, and
`Store` instances. `.attach()` requires `control_app.state.require_node_token` and installs that
authentication dependency on both routes. It sets `control_app.state.training_adapter_extractor`.
Register only on the control app:

- `POST /node/training/adapters/extractions`
- `GET /node/training/adapters/extractions/{command_id}`

POST returns the shared `TrainingAdapterExtractionStatus` after threaded extraction; GET can
observe durably persisted queued/running/terminal state while the control event loop stays free.
Duplicate exact commands return their recorded status; changed intent, reused adapter UUIDs,
reused extraction reservation IDs, or existing final artifacts conflict. Failed commands are
terminal; a new authorized attempt uses fresh command/artifact/reservation IDs.
No shared contract additions are requested.

The extractor **owns its disk reservation**, using the supplied ID with workflow ID equal to
the extraction command ID and variant ID equal to the frozen base variant. Do not pre-hold that
ID with the trainer's payload. The ledger reserves `max_bytes` on the artifact filesystem,
including manifest overhead, and a bounded CPU metadata/copy memory envelope. It applies
`training_artifact_disk_floor_bytes` and `training_artifact_quota_bytes`; persisted artifacts
remain counted after temporary holds release. Existing node ledger external disk accounting
must continue to include other training/import work as provided by its owners.

Intent is fsynced before admission; no artifact staging/copy precedes the hold. Staging is private
and output files are mode 0600. Files and staging directories are fsynced before rename; the
artifact root is fsynced before success. The prepared publication manifest is journaled before
rename. Startup removes interrupted private staging and fails the command, or independently
verifies exactly journaled published bytes and acknowledges success. Exact ledger payload
validation precedes recovered hold release. A post-rename fsync uncertainty retains the running
journal/hold for reconciliation, rather than returning false success or discarding output.

Extraction success is local artifact creation only. The controller owns current authority and
fence checks, replication, reserved Studio inference smoke, cancellation/publication ordering,
registry readiness, and independent harness verification. Existing serving path verification
accepts this output format and checks its exact artifact and base digests.

## Verification

Commands run:

```text
uv run pytest -q apps/coire-node/tests/contract/test_training_extraction.py \
  apps/coire-node/tests/unit/test_training_checkpoint_store.py \
  apps/coire-node/tests/contract/test_exact_adapter_engines.py
# 40 passed; missing test secret directories produced 36 existing Settings warnings.

uv run ruff format apps/coire-node/src/coire_node/training/extraction.py \
  apps/coire-node/src/coire_node/routes/training_extraction.py \
  apps/coire-node/tests/contract/test_training_extraction.py
uv run ruff check <the same three Python files>

uv run mypy --follow-imports=silent \
  apps/coire-node/src/coire_node/training/extraction.py \
  apps/coire-node/src/coire_node/routes/training_extraction.py \
  apps/coire-node/tests/contract/test_training_extraction.py
```

The 28 extraction tests cover all three native configurations, unchanged tensor bytes,
private served metadata, existing `EngineManager.adapter_path()` compatibility, exact retry,
changed-command/reused-artifact conflicts, adapter and optimizer corruption, descriptor
keys/shapes/dtypes, independent native incompatibility despite matching manifests, executable
config refusal, QLoRA missing biases/wrong quantization, DoRA missing magnitude, deadlines
before and during copy, command/store quota refusal, authenticated POST/GET and data-app
exclusion, restart persistence, interrupted staging and published-but-unacknowledged recovery.
Crash injections use actual extraction fsync boundaries and reconstruct the reservation ledger
from disk, confirming no leaked holds.

Constitution: I (native served format), II (synthetic CPU/header-only verification), III (existing
shared contracts), IV (authenticated control-only routes, private inert artifacts), V (exact
acquired base), VI (extraction span, structured correlation logs, bounded-label outcome counter),
VII (spec/plan and meaningful contract/crash tests). Feature-level dashboard/alert integration
is owned by the operations worker; the new counter is
`coire_training_adapter_extraction_outcomes_total`, labeled only by node/state/reason.
This record establishes local extraction verification; physical replication and bare-server
inference acceptance belong to the feature's subsequent Studio gates.
