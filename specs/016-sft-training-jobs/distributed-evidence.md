# Feature 016 — native distributed node evidence and integration handoff

## Current status

The node-side implementation now consumes the main owner's shared collective, rank-component,
grant/import/verification, event and collection contracts. Native two-rank launch is enabled
when the accepted collective binding matches the configured generated hostfile. The real
private transport publishes components, waits for authenticated peer collection, and stages
the common bundle. There is no default/local-success durability callback.

**Production registration in `agent.py`, controller T104 integration, and hardware acceptance
remain the other owners' work.** T102/T103/T104/T106 feature-wide checkboxes remain unchecked
because those integrated acceptance gates have not run. This evidence does not claim full
feature completion or numerical/hardware acceptance.

No shared contract, API/controller, `agent.py`, reservation implementation, network, Studio,
dependency or model configuration files were edited by this work. No commits, model loads or
Metal work occurred. Tests use synthetic CPU safetensors and in-process ASGI/HTTP transports.

## Unchanged pinned upstream source verification

Source was read over HTTPS without importing MLX or loading any model:

- Trainer 0.31.3: <https://raw.githubusercontent.com/ml-explore/mlx-lm/v0.31.3/mlx_lm/tuner/trainer.py>
- Gradient reduction 0.32.2: <https://raw.githubusercontent.com/ml-explore/mlx/v0.32.2/python/mlx/nn/utils.py>
- Launcher: <https://raw.githubusercontent.com/ml-explore/mlx/v0.32.2/python/mlx/_distributed_utils/launch.py>
- Hostfile: <https://raw.githubusercontent.com/ml-explore/mlx/v0.32.2/python/mlx/_distributed_utils/common.py>
- Global group cache: <https://raw.githubusercontent.com/ml-explore/mlx/v0.32.2/mlx/distributed/distributed.cpp>
- Native env: <https://raw.githubusercontent.com/ml-explore/mlx/v0.32.2/mlx/distributed/jaccl/lib/jaccl/jaccl.cpp>
- Array buffer: <https://raw.githubusercontent.com/ml-explore/mlx/v0.32.2/python/src/buffer.h>

`train()` accepts the supplied optimizer, loss, iterator and callback. It calls
`average_gradients(grad)` only at accumulated optimizer-update boundaries, then divides by
accumulation steps, updates the optimizer, clears accumulation and evaluates the state before
reporting. Callbacks execute on **every rank**. Coire does not fork or monkeypatch the loop,
nor reduce gradients a second time. Upstream averages rank-local gradients; this is not a
claimed globally token-weighted training objective. `evaluate()` reduces token-weighted loss
and token counts independently.

Strict `mx.distributed.init(backend="jaccl", strict=True)` registers the backend under `any`
in the native cache. Unchanged trainer/evaluator/gradient helpers therefore use that group.
The worker checks the trainer iterator's group rank and size. Parameter and bundle identity
use `all_gather(..., group=group, stream=mx.cpu)`; pause/stop agreement uses `all_max`.

`mlx.launch` normally uses SSH/shell scripts. Its bare JACCL runtime interface is reused here
through per-node authenticated dispatch instead: `MLX_RANK`, a private `MLX_IBV_DEVICES`
connectivity JSON file, `MLX_JACCL_COORDINATOR`, and `MLX_METAL_FAST_SYNCH=1`. Each coire-node
spawns only its own fixed worker argv in its own process group. No API-side SSH, caller hosts,
control-fabric tensor fallback or new engine port is introduced.

## Implementation interfaces

### Launch and worker

`TrainingSupervisor(..., jaccl_hostfile: Path | None = None, jaccl_coordinator_port=32323)` uses the existing configured
sharding JACCL hostfile. `collective_launch(prepare)` requires the frozen shared binding and
matching runtime, verifies its digest, backend, exactly ordered declared Studio `.fabric`
names, native connectivity matrix and allowlisted fast-sync environment. Native prepare and
start enforce this binding; missing binding/configuration fails closed. The generated matrix
supports native string or string-list device entries and is not hand-authored by the worker.

`JacclLaunch.environment(prepare, owned_directory)` derives the native environment from this
validated inventory. Coordinator DNS is always `coire-edge-a.fabric` and the port is the
accepted existing port, which must also match the node-local coordinator port (the unchanged
bare launcher default is 32323). A wire command cannot widen that port selection.
Start intent precedes process creation; half-spawn retains unknown
ownership and holds. Existing PID/create-time/argv/group stop proof and reconciliation remain.

The native worker constructs `PrivateRankTransport` itself from the node-owned journal,
accepted prepare and private control mailbox. There is no executable/import/path transport
argument on the CLI. It initializes identical model parameters, compares raw evaluated hashes
before updates (including resume), and preserves all per-rank optimizer/RNG/sampler state.
Hashes include parameter names, shapes and dtypes and use 1 MiB contiguous buffer slices,
without a whole-tensor byte copy or BF16-to-FP32 conversion.

MixtureSampler supplies its deterministic rank-local slice, which is not partitioned again.
Compatible SingleSourceSampler advances an identical global sequence and is sliced once.
Held-out sampler state and training RNG are restored after evaluation. Common publication and
restore reject divergent global Single snapshots as well as differing mixture identity/cursors.
The stale mixture/replacement native-validation refusal is removed. Canonical split hashing
still uses `coire_core.training_data.split_digest`.

`DeadlineGuardian` supplies an independently watched <=60-second progress/collective boundary.
The worker's <=0.5-second watchdog enforces it alongside lease, footprint and execution bounds;
renewed leases cannot prolong a deadlocked collective forever. Native supervisor cancellation
retains the existing <=5-second owned process-group lane. Direct library callers must monitor
their guardian independently; a timer cannot itself unwind a blocked native call.

### Component store and transfer

`TrainingComponents(root, journal, peer_addresses=data_peer_addresses)` uses the same artifact
root as CheckpointStore. Components live at
`.rank-components-<common-UUID>/rank-<rank>/`, never as incomplete full artifact manifests.
`register()` writes a strict `TrainingRankComponentManifest` after byte/header/state checks.
`verify()` checks all file hashes/sizes, exact tensor headers, optimizer tree references and
integer step, sampler rank, full attempt/fence/runtime/resolved identity, and current execution
authority. Component descriptor publication is private and fsynced.

Grant control uses `TrainingRankGrantRequest` and returns `TrainingArtifactGrantIssued`.
The short-lived grant ledger uses hashed secrets/constant-time comparison and exact component,
file-ID/byte/expiry/destination-peer scope. Reads recheck the live attempt and fence. Full
artifact manifest/grant behavior stays strict and separate. Secrets are never journaled.

`RankImporter(components, journal_root, port=..., client=..., reservations=...)` accepts the
strict shared import request. It transfers only via `DataFabricClient` to the declared source
`.fabric` endpoint, with no redirects, proxies or control fallback in its native client. It
checks byte bounds, content encoding, exact Range responses, checksums and full rank state
before atomic publication. Imports have a fixed 60-second monotonic deadline that refresh
cannot extend; cancellation/expiry/restart never resurrect a transfer. Failed partial files
can resume with a scoped refreshed grant inside the original deadline.

The optional `reservations` argument binds production imports to the existing ReservationLedger:
192 MiB transfer memory and conservative complete-component disk holds, exact persisted scope
UUIDs, require-stop semantics, and release only after streams and owned filesystem threads drain.
On node-agent restart those exact journaled scope IDs can be reaped because the importing
coroutine/threads do not survive the agent. Import scope IDs use the existing default owner
kind, so they do not collide with `AccountedArtifactImporter`'s full-artifact reaper. Tests may
omit the ledger; production registration must pass it. Retained partial bytes remain on disk
and count in the existing physical/quota projection. The importer does not free training holds.

Credential-free import journals persist shared intent/status plus local reservation IDs.
Task/status caches are bounded. A cancelled or restarted pending import cannot be silently
respawned. Its status remains observable and a controller chooses explicit recovery.

### Controller-to-worker ordering (no mirror-before-event deadlock)

1. Each evaluated rank saves its full component through `save_rank`.
2. `PrivateRankTransport.publish()` registers the component and persists the typed
   `NodeRankCheckpointPayload(kind="checkpoint_rank_staged")` event.
3. Controller issues source grants and starts peer imports on both destinations, then polls
   actual verified statuses/receipts. Tensor bytes stay Studio-to-Studio.
4. Controller sends the same two descriptors through `TrainingRankCollection` to each node.
   `publish_collection()` verifies both local components before writing the owned mailbox.
   It rechecks fence/lease/liveness/update after verification; expensive verification does
   not hold the supervisor cancellation lock. No grant secrets or caller paths enter the mailbox.
5. Both workers verify the mailbox and component bytes, assemble a complete bundle through
   `publish_rank_bundle`, and compare the common canonical manifest digest via bare collective.
6. The workers emit `NodeCheckpointPayload` as **staging**, allowing the controller to verify/
   mirror the complete bundles. They do **not** wait for mirror receipts before this event.
7. Only the existing fenced `CheckpointCommitAcknowledgement`, after the controller verifies
   both full copies, permits progress. All ranks agree pause/stop after acknowledgement too.

The journal validates rank event scope/digests and rejects stale update rewinds. Worker event
sequencing includes both rank and full-bundle events. Scoped commands include the frozen request
digest. No additional receipt envelope/shared contract is required by this staging design.

## Registered-by-attach route contract

`TrainingComponents.attach(control_app, data_app, importer=rank_importer)` installs bearer
authentication on the control router and peer-grant authentication on the data router only.

| Control path under `/node/training` | Contract |
| --- | --- |
| `POST /attempts/{id}/rank-collection` | TrainingRankCollection -> NodeTrainingStatus |
| `GET /components/{artifact}/ranks/{rank}` | TrainingRankComponentManifest |
| `POST /components/{artifact}/ranks/{rank}/verify` | TrainingArtifactVerifyRequest -> TrainingRankVerificationReceipt |
| `POST /components/grants` | TrainingRankGrantRequest -> TrainingArtifactGrantIssued |
| `DELETE /components/grants/{id}` | revoke, 204 |
| `POST /components/imports` | TrainingRankImportRequest -> TrainingRankImportStatus, 202 |
| `GET /components/imports/{id}` | TrainingRankImportStatus |
| `POST /components/imports/{id}/grant` | TrainingArtifactGrantRefresh -> TrainingRankImportStatus, 202 |
| `POST /components/imports/{id}/cancel` | TrainingRankImportStatus |

Data-only paths: `/training-components/{artifact}/ranks/{rank}/manifest` and
`/training-components/{artifact}/ranks/{rank}/files/{file_id}`. They require
`X-Coire-Artifact-Grant` and the authorized destination's resolved data peer address; files
support the existing FileResponse Range behavior and private/no-store caching.

## Exact agent/main-owner integration handoff

The `agent.py` owner should:

- Pass `jaccl_hostfile=Path(settings.sharding_jaccl_hostfile)` to TrainingSupervisor.
- Construct one RankImporter from `training.components`, a private distinct journal directory
  such as `<node_state_dir>/training/component-imports`, `settings.node_data_listen_port`,
  existing quota/floor settings, and **the shared reservations ledger**.
- Call `training.components.attach(control_app, data_app, importer=...)` once each listener
  app exists, with `control_app.state.training` and settings already attached. Include importer
  shutdown before closing the native training journal. Equivalently wire the two routers and
  their documented app-state attributes in `create_app` when apps are constructed separately.
- Keep hidden component/staging paths in retained artifact quota and operational cleanup
  accounting; they are real artifact bytes, not source inputs or committed serving adapters.

Controller main owns both-ready start dispatch, rank-event polling, scoped peer imports/
refresh/cancel, collection commands, complete-copy verification, both-rank commit and coordinated
stop/death/recovery fencing. Start/stop/lease command builders that derive from prepare must
exclude the new `collective` prepare-only field (the native watchdog builder was updated here).
No API/controller/shared-schema edits were made here.

## Concrete physical prerequisite

The endpoint-preserving RDMA trial is suspended after failed peer verification/rollback.
No helper was run. Hardware acceptance requires an already working deployment-managed native
JACCL hostfile on both Studios with the accepted digest, actual discovered RDMA peer devices,
declared `.fabric` coordinator reachability on its existing allowed port, and a successful
bounded two-rank bare all-reduce. Inventory `--check` alone is not a collective success test.
Do not run `--auto-setup`, change addresses/routes/firewalls or repair the suspended fabric
as part of this implementation.

## Verification

Focused CPU gate: **119 passed, no skips** across component contracts, native lifecycle,
distributed simulation, mixture compilation, checkpoint store, worker control and both sampler
suites. Twelve new component tests cover actual ASGI component byte transfers and both local
copies/collection mailboxes, credential-free journals, scope/revocation, grant refresh with Range,
cancellation, corruption, new-attempt fencing, deadline/restart refusal, native rank env/half-spawn
holds, control/data authentication separation, typed rank events/wrong-rank refusal and real ReservationLedger drain/release.
Three benign test-only missing secret-directory warnings (`/run/secrets`, `/nonexistent`).

Test command executed:

```sh
uv run pytest -q \
  apps/coire-node/tests/contract/test_training_components.py \
  apps/coire-node/tests/contract/test_training_lifecycle.py \
  tests/integration/test_training_distributed.py \
  apps/coire-node/tests/unit/test_training_mixture_worker.py \
  apps/coire-node/tests/unit/test_training_checkpoint_store.py \
  apps/coire-node/tests/unit/test_training_worker_control.py \
  apps/coire-node/tests/unit/test_training_sampler.py \
  apps/coire-node/tests/unit/test_training_sampler_basic.py
```

Strict mypy passed for all seven owned node modules. Ruff check and formatting checks passed
for those modules and the component/lifecycle/distributed tests. Observability uses
`coire.node.training.component.import`, `coire.node.training.rank.checkpoint`, bounded component
import/rank coordination outcome metrics, existing node command telemetry and content-free
job/attempt/update logs. Broader feature stall/checkpoint/uncertainty alerts and Jobs panels
remain the operations owner's surfaces. Constitution I–VII checks retain the bare runtime,
Studio-only tensors, typed/authenticated/fenced interfaces and honest acceptance boundary.
Final scoped `git diff --check` passed. No post-implementation extension hooks are configured.
