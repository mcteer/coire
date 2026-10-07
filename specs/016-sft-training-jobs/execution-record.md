# Feature 016 — Execution record

## Session 2026-10-03: baseline and setup

Implementation started on `feat/016-sft-training-jobs` at
`6c1fccccd6fd533f3d8793d88336c20bd8fc25c6`. Planning artifacts were already uncommitted;
they are retained. No commits, pushes or PRs are authorized in this session.

### Prerequisite and checklist results

- Spec Kit resolves `specs/016-sft-training-jobs/tasks.md`; continuous implementation bound to
  that file. Requirements checklist: 16 checked, 0 unchecked. No extension hooks configured.
- `git rev-parse --git-dir`: Git repository; selected branch confirmed.
- `uv --version`: 0.12.7; Python 3.13.15 on arm64 Darwin.
- `uv run --frozen alembic -c apps/coire-api/alembic.ini heads`:
  `0030_image_output_retention (head)`, one head.
- Installed package metadata: MLX 0.32.2, mlx-lm 0.31.3, Pydantic 2.13.5, pytest 9.1.1.
  Metadata lookup imports no engine and loads no weights.
- `uv run --frozen pytest -q packages/coire-core/tests`: **273 passed**; 17 existing
  warnings about intentionally absent test secret directories. No skipped core tests.
- Docker server 29.4.0 is available. Existing `coire` containers are live; training integration
  fixtures must use their own disposable database/container names and must not alter production.

### Placement prerequisite

The execution host reports `coire-core.lab`. No model/Metal workload may run here (Principle II).
`COIRE_TEST_MODEL` is not configured. Numerical validation requires an authorized non-core Mac
or Studio and an admin-acquired local <=1 GB fixture. The fixture must fail closed when an enabled
engine test runs on core or lacks its local asset, not download or count a skip as success.
Studio validation uses documented authenticated workflows; this is not a prohibition on those tests.

### Ignore setup

Verified Python/Vite/secret/runtime patterns. Added the existing pnpm store and root data/blob
artifacts to Git/Docker ignores plus bounded generic temporary-file patterns. Added web Prettier
ignores for outputs/dependencies/lockfiles. ESLint flat config already ignores outputs and caches;
the web package is private, so no npm publishing ignore is needed. No Terraform or Helm project
was found. Model/dataset/checkpoint data remains excluded from Git and build contexts.

### T002–T003 verification

- Added small synthetic JSONL fixtures in all supported formats, private local mirror roots and
  an on-demand dedicated loopback Postgres 17 container fixture. It does not reuse production
  credentials/databases. The verification created and removed its own randomly named container.
- Enabled numerical tests fail on core, missing absolute local assets or absent/mismatched
  acquisition checksums. Unrequested fixtures perform no model loading/download or DB startup.
- `uv run --frozen pytest -q tests/test_training_fixtures.py apps/coire-node/tests/unit/test_training_fixture.py`:
  **8 passed**, including a real isolated Postgres 17 connection. No skips.
- Changed-file Ruff check/format: pass. Strict mypy on fixture helper and tests: pass (3 files).
- `git diff --check`: pass. `git check-ignore` confirms pnpm/model/data/blob exclusions.
- ADR 0012 records all Constitution principles, bare API boundaries, state/replication/identity
  decisions, pause/cancel deadlines and unchanged numerical/coexistence gates. No exception.
- T001–T003 complete; numerical training/model acquisition has not been executed.

### T004–T009 shared contracts

- Recorded red test run: new contract collection failed with missing `coire_core.models.adapters`.
  Implemented strict dataset/training/adapter/node models and compatible canonical tool content.
- Added finite/size/depth/unknown-field rules, immutable input/target digests, full rank/file
  checkpoint manifests, exact execution-scoped grants and typed progress/measurement results.
  Image-bearing training rows, malformed tool relationships, duplicate split leakage, unsafe
  selectors and under-sampled successful coexistence claims are rejected.
- `uv run --frozen pytest -q packages/coire-core/tests`: **312 passed**, including 39 new
  training contract cases. No required test skipped.
- Strict mypy on all six affected schema modules and tests: pass (7 files); changed-file Ruff pass.
- OpenAPI and web TypeScript types regenerated after the conversation contract change.
- Full non-integration/non-engine regression: **1,938 passed, 2 unrelated existing skips,
  176 deselected**. These results do not close numerical/Studio acceptance gates.
- Metadata-only prerequisite checks: legacy admin credential returns 401; prior development key
  returns 429; the existing benchmark admin key can read the registry (200). Credentials were
  retrieved inside the process and never printed. Ready tiny text bases already exist in the
  registry, including SmolLM-135M and Qwen2.5-Coder-0.5B; no new acquisition was triggered.

### Setup fixture correction and final checks

- Added the existing in-process node harness as a disposable authenticated fake-node fixture;
  inference command is fixed to the fake engine and Hub access is offline. It starts no model.
- A repeat database fixture run exposed an initialization race: socket-only `pg_isready` can
  accept the image's temporary init server before its final TCP listener starts. Readiness now
  checks TCP explicitly; production/database checks are not relaxed.
- Offline fixture verification now checks the entire inert file tree against its acquisition
  manifest and rejects unlisted files or symlinked directories before numerical use.
- Final focused setup/schema verification: **49 passed, 0 skipped**. Strict mypy on all changed
  source and test modules: pass (10 files). Full workspace mypy: pass (661 files) before the
  final focused fixture addition; the additional modules pass the focused strict check.
- Web tests: **144 passed**. Web ESLint and production TypeScript/Vite build: pass.
- Generated OpenAPI freshness and Git whitespace check: pass. Regeneration produced no drift
  in checked-in OpenAPI/TS because the canonical conversation type is not yet an exposed API
  schema; T010/T031 will connect the new target/training projections.

## Concrete blocker: Studio A RDMA setup requires Recovery OS

Authenticated read-only cluster preflight confirmed both Studios healthy but no current link
measurement. The authorized `POST /api/v1/admin/links/studios/probe` returned **409** with
`generated jaccl hostfile is unavailable`. Studio-side capability checks reported:

- `coire-edge-a`: `rdma_ctl status` -> **disabled**.
- `coire-edge-b`: `rdma_ctl status` -> **enabled**.
- Both `.fabric` endpoints resolve from Studio A; control-plane reachability is healthy.

The user explicitly approved documented RDMA capability/hostfile setup. The attempted commands
on Studio A returned:

- `sudo -n rdma_ctl enable`: `sudo: a password is required`.
- `rdma_ctl enable`: **`This tool needs to be executed from Recovery OS.`**

No capability, firewall, network, node runtime or production model state was changed. This session
cannot boot/interact with Studio A's Recovery OS using the authenticated node/SSH workflow.
Operator action is required: enable RDMA on Studio A from Recovery OS, return it to normal boot,
then run the repository's Studio-side generated JACCL/ring hostfile procedure and install the
copies as described in `docs/runbooks/sharded-serving.md`. Do not hand-author RDMA device fields.
The existing audited link probe must produce current success before two-rank acceptance.

T001–T009 are verified and checked. T010–T124 remain unchecked; there is no runnable training API,
scheduler workflow, adapter serving, or completed engine/cluster acceptance yet. Work is paused
at this required hardware prerequisite review, not declared complete. Continuous mode stays on
but the controller is blocked pending the Recovery-OS setup. All work remains uncommitted.

### RDMA restoration and follow-up diagnosis

The user reported RDMA re-enabled on Studio A. Reverification confirms `rdma_ctl status` is
**enabled on both Studios**, and `ibv_devinfo -d rdma_en5` reports **PORT_ACTIVE** on both.
The Recovery-OS prerequisite above is resolved. It is not a remaining user setup request.

Further checks identified separate runtime/deployment facts:

- Both Studios currently run macOS 27.0 build 26A428. Studio B's update history records macOS 27
  installation on 2026-09-26. This does not prove why Studio A's capability had been disabled.
- Core's API points at `/run/coire/cluster/{jaccl,ring}-hostfile.json`. Its read-only bind source
  is the installed immutable release's `cluster` directory, which contains only `.gitkeep`.
  No generated hostfiles were found in the existing core release generations or Studio state.
  `scripts/coire-deploy.py` copies the selected `COIRE_CLUSTER_CONFIG_DIR`; the current source
  `deploy/cluster/generated` is also empty. The evidence establishes absent deployed inputs,
  not when or why prior operator configuration disappeared.
- Studio A's `mlx.distributed_config` console script points at a deleted staging interpreter.
  Invoking its installed entry point with `/opt/coire/envs/current/bin/python3` works and exposes
  the pinned CLI. No production environment or script was patched in place.
- Read-only native JACCL inventory generation over the existing declared `.fabric` endpoints
  currently refuses Studio A's self-SSH connection. Its missing self-alias host trust was enrolled
  only after byte-for-byte comparison with `/etc/ssh/ssh_host_ed25519_key.pub` over the already
  authenticated control connection. No existing known-host entry was replaced or checking disabled.
  The remaining self-SSH error is `Permission denied (publickey,password,keyboard-interactive)`.
  Studio A -> Studio B and Studio B -> Studio A data-alias SSH connections succeed. No authorized
  key, private key, firewall, IP assignment or networking privilege was changed.
- The attempted native discovery used `--no-auto-setup`, so it did not run the upstream IP/bridge
  reconfiguration. RDMA device fields must still be obtained from native discovery, not guessed.

The concrete Recovery-OS hardware blocker has been removed. Hostfile provisioning and existing
launcher/authentication integration need resolution before the distributed gate; they do not
establish a failure of the now-active RDMA link. T010 remains the next unchecked implementation
task. The controller reports implementation inactive after the intervening user messages.

Final repeated focused verification with `COIRE_INTEGRATION=1`: **49 passed**, no skips.
The entire workspace Ruff check passes; a new test-decorator formatting issue found by the full
format check was fixed. No failing numerical gate was bypassed or declared passed.

## Resumption: T010–T011 and command aliases

The user requested removal of redundant OpenCode `speckit-*` registrations and explicitly
directed resumption of feature implementation. Updated the global Spec Kit plugin and its docs/
tests (outside this repository). Ten plugin tests pass; fresh registration has `spec` plus the
ten dotted Spec Kit commands and no hyphenated aliases. A new process is required to refresh the
current OpenCode command menu. The already-loaded controller pauses on ordinary user messages;
implementation is continuing normally under the user's explicit resumption instruction.

- Added typed exact-target projections to existing engine/instance/harness/run/auth/chat/MCP/
  console/node boundaries. Legacy run scopes have no exact adapter grant. Exact targets are frozen
  and duplicate/out-of-parent grants are refused. Public selector acceptance and adapter subject
  submission stay closed until their actual routing/evaluation tasks implement the exact path;
  the old evaluator explicitly rejects an adapter subject rather than silently scoring the base.
- Added default-off training config, all resource/queue/storage/control/evidence bounds and typed
  RFC 9457 domain errors. Every new runtime environment variable is documented in compose README.
- Core tests: **331 passed**. Strict workspace mypy: **662 files**, pass. Ruff check/format pass.
- Broad Python regression discovered 4 failures: historical strict node schemas rejected empty
  new projections, and the failover OpenAPI snapshot was stale. Legacy wire shapes now omit empty
  training/target fields; populated projections remain typed. Generated API/failover OpenAPI and
  web TS types were updated. Focused affected/new contracts: **92 passed**; none skipped.
- Web fixture updated for default-off training capability; build, lint and **144 web tests** pass.
- OpenAPI/failover freshness and Git whitespace checks pass. No existing test/assertion/validator
  was disabled or relaxed to resolve the failures. T010–T011 are checked; T012 is next.

### T012–T016 persistence, authority and recipe execution metadata

- Implemented the frozen, reversible `0031_sft_training` migration and ORM models for inputs/
  analyses, jobs/attempts/participants, checkpoints/adapters/copies, references, commands,
  grants, storage holds, measurements/profiles, events/losses and eviction restoration intent.
  Added nullable exact-subject fields to existing inference/run/chat/evaluation metadata.
- Recorded the initial failing schema test run before implementation. Real disposable Postgres
  tests now exercise unique intent/output/attempt/adapter identities; the actual Alembic chain
  upgrades through 0030 to 0031, verifies all new table columns, preserves a legacy usage row with
  null target fields, refuses live-job downgrade and source-intent edits, then performs a clean
  downgrade/re-upgrade. No production database was touched.
- Live admin/user/key/origin checks exclude node/run/ops/service and legacy identity-free
  principals. Generic command receipts and mandatory audit share the mutation transaction.
  Tests prove retries/conflicts, one audit row per accepted command, and complete rollback when
  audit writing fails. Credential rotation and role revocation override stale principal claims.
- Ordered events and immutable loss samples persist in Postgres with exact attempt fencing and
  bounded cursor pages. Expired event cursors return content-free snapshots. No original YAML,
  source row, grant secret or model bytes enter event telemetry. Diagnostic storage is not used
  as the loss/history source of truth.
- Safe recipe loader preserves source bytes separately from normalized typed settings and hashes
  only client-declared intent before registry/operator defaults. It rejects duplicate keys,
  aliases/anchors, executable/binary tags, depth/size overflow, unknown fields and nonfinite or
  boolean optimizer numbers. Unquoted scientific notation uses a local resolver; global YAML
  parsing behavior is unchanged. Form/YAML mismatches fail without echoing sensitive input.
- Focused core/authority/recipe selection: **355 passed**, no skips. Real Postgres migration/
  command/event/metric selection: **9 passed**, no skips. Strict mypy: **673 files**, pass.
  Changed-file Ruff and API/failover freshness/Git whitespace checks pass. T012–T016 are checked;
   native runtime and all remaining feature acceptance tasks are still incomplete.

## Session 2026-10-04: native foundations and real numerical evidence

- Resumed the existing uncommitted runtime/artifact work. The requirements checklist remains
  16/16; there are no extension hooks. The Spec Kit controller reports continuous implementation
  inactive, so ordinary implementation proceeded under the user's instruction.
- Initial focused loader/rendering/sampler/checkpoint/transfer suite: **29 passed**, no skips.
  Full strict mypy found four dataset-normalizer typing errors; distinct row variables and the
  canonical parts union correct those errors. Full static verification then passed (694 files).
- Studio A is reachable through the declared control DNS and runs MLX **0.32.2**, mlx-lm
  **0.31.3**. Staged source and isolated pytest dependencies under `~/coire-stage/016-runtime-gate`
  and `~/coire-stage/016-test-deps`; no installed node environment or model bytes were replaced.
  No model/tokenizer/Metal work ran on core.
- Verified the already-acquired local tiny Qwen2.5-Coder-0.5B 4-bit/g64 fixture and its adjacent
  acquisition manifest before loading. Initial real loader test exposed a harmless duplicated
  `quantization_config` alias. Added a red regression and now accept the alias only when exactly
  equal to `quantization`; conflicting/legacy methods still fail preflight. No acquired config
  was edited. SmolLM's tokenizer is outside the initial allowlist and was refused; it was not
  silently admitted or used as numerical evidence.
- The actual offline runtime gate on Studio A used:
  `COIRE_TRAINING_ENGINE=1 COIRE_TEST_MODEL=/opt/coire/models/mlx-community--Qwen2.5-Coder-0.5B-Instruct-4bit.mcp-acceptance`
  with the versioned interpreter and staged source paths, running the three
  `apps/coire-node/tests/engine/test_training_{runtime,resume,rendering}.py` files:
  **11 passed, 0 skipped**, in 12.39 s. The isolated staging tree lacks root pytest marker
  registration, producing three unknown-marker warnings; this does not change assertions.
- Each of three recovery trials (seeds 42, 43, 44) ran 36 completed updates with rank-2 QLoRA,
  nonzero dropout 0.15, accumulation 2 and a warmup/changing linear learning rate. The unchanged
  upstream callback unwound at update 4, after full state serialization. A new runtime restored
  exact adapter/optimizer arrays, step, sampler and current MLX key, then continued 32 updates.
  All subsequent sample/RNG identities matched exactly; weights/moments met `rtol=1e-5,
  atol=1e-6`, and losses met `rtol=1e-4, atol=1e-5`. Destination key/shape/dtype mismatch and
  reset/missing optimizer moments were refused. This is numerical callback/checkpoint evidence,
  not a claim of completed process supervision or real Studio-reboot acceptance.
- Actual serving primitives matched training for Unicode, multipart text, tool-only turns,
  tool responses, final tool-call targets, both thinking settings and override template content.
  Provenance metadata was excluded. Base weights remained unchanged after the real optimizer step.
- Safetensors/header corruption, immutable local commit/retention, scoped peer grants, Range,
  refresh/revoke, independently verified atomic imports and listener separation passed their
  local unit/contract checks. These checks do not establish physical two-Studio replication.
- Combined core/runtime/artifact/base-serving/dataset tests: **387 passed**, no skips, before
  the additional explicit dense-parameterization metadata test. Full mypy (694 files), Ruff
  check/format (1,383 files) and Git whitespace check passed at that point.
- T017–T025 are checked. T026–T124 remain unchecked, including served LoRA/DoRA, dataset/API/UI,
  orchestration, all process/reboot/distributed/coexistence and release gates. Training remains
  default-off and feature 016 is not complete.

### Authorized self-SSH correction and native discovery prerequisite

The user explicitly authorized enrollment of Studio A's existing public key for self-SSH.
Confirmed that the Ed25519 key had no existing matching authorized-key entry. Appended one
`restrict` entry scoped to the exact addresses resolved from the declared `.fabric` alias;
host-key checking remains enabled. The enrollment helper is outside the repository. The
existing public-key fingerprint is `SHA256:742lpt4hfS2z0N90xn/yh5EUsKFmt9kxhLhNcp+DqvE`.
`ssh -o BatchMode=yes mcteer@coire-edge-a.fabric true` now succeeds from Studio A.

Native `mlx.distributed_config` was invoked through the installed console entry point with
`--backend jaccl --hosts coire-edge-a.fabric,coire-edge-b.fabric --over thunderbolt
--no-auto-setup --output-hostfile /opt/coire/state/jaccl-hostfile.json`.
It successfully discovered connectivity/interfaces and RDMA on both Studios, but then requires
interface setup before saving the generated hostfile: lower `bridge0`, assign an individual
`en5` interface a generated /30 network, and change its peer route. The existing declared
replication endpoints use the bridge's `192.168.100.*` network instead. No printed `ifconfig`
or `route` command was executed; no bridge/address/firewall was changed. The noninteractive
command exits with EOF at its setup confirmation, and no usable hostfile is claimed.

The existing-key approval does not authorize replacing the Studios' data-fabric addressing.
Finishing native discovery requires an explicitly approved compatible network setup/rollback
procedure preserving declared replication endpoints, followed by measured authenticated link
probes. Do not guess device fields or mark the distributed gates complete from discovery alone.

Final focused verification after the dense-matrix metadata test: **388 passed, no skips**;
full strict mypy **694 files** passes; Ruff check and format **1,383 files** pass;
generated API OpenAPI freshness and Git whitespace checks pass. All work remains uncommitted.

## Approved endpoint-preserving fabric preparation (2026-10-04)

The user explicitly approved preparing and applying compatible Studio data-fabric setup with
rollback while preserving declared replication endpoints. The network-approval prerequisite
above is resolved; remaining application prerequisites must not be described as missing approval.

### Discovery and prepared deployment helper

- Pinned native MLX `extract_connectivity` and `IPConfigurator` identify **en5 on both ends**
  of the direct Studio connection. Both `ibv_devinfo -d rdma_en5` observations report
  `PORT_ACTIVE`; both `rdma_ctl status` observations report enabled.
- Both current endpoint addresses are on active `bridge0`, with the same /24 netmask. The
  direct interfaces are bridge members and have no IPv4 addresses. Core/control default routes
  are on Wi-Fi `en1`. This establishes the exact pre-cutover state without changing it.
- Added deployment-managed `deploy/cluster/scripts/studio-rdma-fabric.py`. Read-only checks
  verify the native interface, bridge membership, unique source address and declared DNS.
  Application first saves an immutable fsynced private baseline, then moves the **same**
  address/netmask to the direct interface. Every operation uses explicit argv. A failing
  command/verification restores original membership, bridge address and state. Explicit
  rollback verifies the restored state and retains the baseline.
- The helper also calls unchanged native MLX JACCL/ring hostfile generators with an explicit
  network-setup object that verifies the already-configured direct fabric. It preserves native
  RDMA device fields and pins side-channel/ring addresses to declared `.fabric` endpoints.
  There is no MLX global/function monkeypatch, guessed device matrix, new subnet, firewall rule,
  sudo-policy change or control-fabric data fallback.
- This is a bounded **runtime cutover trial**. Persistent macOS network-service configuration
  and reboot validation remain outstanding and are not implied by the helper or a future
  successful runtime probe. The runbook and plan record that limitation explicitly.
- Unit/script tests: **17 passed, no skips**. Tests cover preserved addresses, selector refusal,
  immutable/private baseline, failure after each cutover step, verified restoration, required
  direct-route/bridge-down evidence and unchanged native-generated device fields. Focused strict
  mypy (2 files) and Ruff check/format pass; Git whitespace check passes.
- Staged the tested helper as `~/coire-stage/studio-rdma-fabric.py` on **both Studios**.
  Real `--check --interface en5` succeeds on both and displays endpoint-preserving plans and
  rollback commands. No network mutation occurs in check mode.

### Concrete application blocker: operator-authenticated sudo

Attempted the exact staged application command via the existing authenticated control connections:

```text
sudo -n /opt/coire/envs/current/bin/python3 /Users/mcteer/coire-stage/studio-rdma-fabric.py --apply --interface en5
```

**Both Studios refuse with `sudo: a password is required`.** No interface/address/route change
or rollback snapshot was made. Application cannot be completed through the current noninteractive
SSH credential. Do not weaken sudo policy or put an operator password in files, chat or logs.
An operator can run the staged command in an interactive Terminal on each Studio using their
own sudo authentication. Drain existing distributed/replication work before doing so, as in the
runbook; use control `.lab` SSH when invoking remotely.

Authenticated post-attempt `/node/health` returns **200, path=control** on both Studios.
Authenticated `/node/data-link` returns **200, ip_state=up, rdma_state=degraded** on both.
The degraded RDMA projection is not a failed native-port observation; no valid current
collective evidence has been recorded. A real `--generate jaccl` attempt refuses because the
bridge is still up, before creating a hostfile. No collective or transfer success is claimed.

After root-authenticated cutover on both peers, generate/install matching hostfiles through
the documented deployment workflow, run authenticated link and transfer verification, then
measure runtime rollback and persistent/reboot behavior. Feature-016 tasks remain incomplete;
the blocker is now the concrete root credential, not permission to prepare the network.

## Post-application verification and route correction (2026-10-04)

The user reported running the requested cutover commands on both Studios. Read-only verification
confirms both original /24 addresses are present on `en5`, `bridge0` is down without the moved
address/member, and both RDMA ports remain `PORT_ACTIVE`. The private root-owned rollback
directory exists on both; the agent cannot read its contents through the unprivileged connection.

### Verification found a deployment-helper defect

The routes to both the peer and the node's own `.fabric` address select the Wi-Fi `en1` default
route. `netstat` contains no connected data subnet route, and scoped `route get -ifscope en5`
returns `not in table`. Both peer `.fabric` SSH connections and Studio A self-SSH time out.
Authenticated `/node/health` remains **200, path=control** on both, while authenticated
`/node/data-link` reports **ip_state=down, rdma_state=degraded** on both.

The original helper was incomplete: it relied on an automatically-created connected route and
verified only immediately after `ifconfig`. It must not be described as a successful working
fabric merely because the interface address moved. The route disappearance is measured; the
exact macOS service/daemon that removed it has not been established.

Added a failing regression that models an initially valid connected route disappearing after
the first read. The corrected helper explicitly installs only the local /32 loopback route and
the peer /32 direct-interface route, refusing unexpected existing host routes and leaving all
default/control routes untouched. It verifies address, bridge-down state and both static host
routes in five samples over eight seconds. New snapshots record existing static-route provenance;
the original snapshots remain readable without rewrite. The earlier observed bridge baseline
had dynamic local/peer routes, not pre-existing static routes.

The new `--repair-route` action loads the retained root baseline, verifies the already-moved
address and unchanged declared peer, completes only the two host routes, and preserves original
rollback data. Failed installation/verification restores the baseline; rollback removes helper
host routes while preserving pre-existing static routes recorded in newer snapshots.

Root remains unavailable through the agent's control SSH (`sudo -n true` returns
`sudo: a password is required` on both Studios). The corrected helper must be staged and
root-authenticated by the operator before native hostfile generation or collective/transfer
tests can proceed. No usable hostfile, successful replication or completed feature gate is claimed.

Final correction verification: **32 unit/script tests passed, no skips**; focused strict mypy,
Ruff check/format and Git whitespace check pass. Tests now cover failures through address change,
both host-route installations and initial verification, removal of introduced routes on rollback,
legacy baseline compatibility, idempotent repair without address replay, delayed/transient route
refusal, and refusal to overwrite a host route on an unexpected interface/gateway.
The corrected helper is staged on both Studios. The exact noninteractive `--repair-route`
application was attempted on each; both refused with `sudo: a password is required` before
executing the helper. Operator-authenticated repair (or the existing rollback command) is required.
Do not rerun the old initial `--apply` command over the retained baseline.

## Post-repair verification: Ethernet route construction (2026-10-04)

The user reported running `--repair-route` on both hosts. Both now have static local /32 routes
through `lo0` and peer /32 routes through `en5`; Studio A's self-SSH succeeds. However, both
peer SSH connections still fail with `No route to host`, both source-bound ping trials lose
all three packets, and authenticated `/node/data-link` remains down on both. Control health
continues to return 200.

ARP and `netstat` establish a second helper defect: on Studio A, the peer address is permanently
mapped to Studio A's own `en5` MAC (`36:f9:75:b7:65:cc`), and on Studio B, the peer address is
permanently mapped to Studio B's own `en5` MAC (`36:b4:6b:0e:32:cc`). These are not neighbor
discoveries or proof of a working link. The earlier tests and route-flag-only acceptance missed
the incorrect link-layer gateway.

Apple's `network_cmds` route(8) specifies that an Ethernet direct route's `-interface` gateway
is this host's address on the common network; the interface-name form is for point-to-point
links. Corrected peer route construction to `-interface <local-address> -ifp <native-interface>`.
Added a red regression proving the former helper silently retained the self-MAC route. The
updated helper replaces only the observed defective helper route, refuses a baseline-owned
static route or an unexpected interface, and checks a resolved non-self Ethernet neighbor
on the intended interface during all delayed samples. ICMP is bounded and primes ARP; an
unsuccessful echo is never a TCP/fabric acceptance result. Cross-node authenticated checks are
still required once both corrections are applied.

The updated source is staged on both Studios. Standard macOS administrator authorization via
`osascript ... with administrator privileges` was attempted for the exact staged repair action
on both hosts; both remote sessions returned authorization error **-60007**, and no successful
root execution is claimed. No credentials were read, recorded or printed. Operator-authenticated
Terminal execution remains necessary. Read-only route debug mode also requires root on the
installed macOS version; no dry-run route mutation was performed.

Current data-fabric acceptance remains **failed**, not a passing feature prerequisite. Native
hostfile generation, collective tests and real peer transfers remain pending until actual
authenticated peer connectivity succeeds. The original rollback baseline is retained by design;
the updated repair does not overwrite it and rollback remains available through operator sudo.

Final verification of the Ethernet correction: **37 unit/script tests passed, no skips**;
focused strict mypy, Ruff check/format and Git whitespace checks pass. New negative controls reject
self-MAC, unresolved and wrong-interface neighbors even with valid-looking route flags, and refuse
mutation of a bad static route recorded as belonging to the original baseline. Local source and
both staged copies have SHA-256
`ad5a915b5d7afc0f13e8aeff9c2bdc92e6657a5d8ce7726ea743b2725306c10f`.
Authenticated final checks still report control health 200 and data-link down on both Studios.
The corrected Ethernet route command has not yet run with operator/root authentication; these
unit results do not replace real peer TCP, authenticated transfer or collective evidence.

## Failed repair and incomplete rollback on Studio A (2026-10-04)

The user supplied the real failure: `verify_direct` refused the route/neighbor state, then rollback
failed at `/sbin/ifconfig bridge0 addm en5` with exit status 1. The original helper captured but did
not display that command's stderr, so the exact kernel rejection reason remains unknown. Do not
attribute it to a guessed driver/permission error from the return code alone.

Read-only incident verification establishes:

- Studio A: `en5` is active but has no IPv4 address; `bridge0` lacks both `en5` membership and its
  original IPv4 address. Both data-address lookups select the Wi-Fi default route. Trial host
  routes were removed before the rollback failed.
- Studio B: its original address remains on direct `en5`, with the earlier static local/peer
  routes. The user was asked to hold B's repair; no further trial was applied there.
- Both Studios retain the original saved SystemConfiguration bridge members `en2`–`en7`, including
  `en5`. Both original Thunderbolt Bridge services remain Manual with the declared /24 endpoints,
  no router, and Enabled. No persistent address or bridge-member rewrite is evidenced.
- Authenticated control health is 200 on both; both authenticated data links remain down.

Suspended direct-interface apply/repair CLI actions before further mutation. Added bounded original
OS stderr to subprocess failure diagnostics. Recovery now validates the saved bridge against the
baseline before cleanup, and avoids retrying the failed raw bridge-member ioctl. Unit cases cover
changed-persistent-config refusal before mutation, re-enable on disable failure, and refusal to
claim recovery when bridge membership is still absent.

Attempted the constrained OS-managed service reactivation on **A only** using the administrator
account and the existing saved configuration. `networksetup` off/on ran without a sudo prompt.
Real verification still failed: the bridge became UP/RUNNING and the service is Enabled, but
`en5` membership and the IPv4 address were not restored. Further automatic mutation stopped.
This is a measured recovery failure, not a successful fallback or an authorization blocker.
The missing port/address, intact persistent configuration and missing original `addm` stderr
remain the concrete investigation inputs. No hostfile/collective/training gate is complete.

Final focused recovery-code verification: **43 unit/script tests passed, no skips**, strict mypy
and Ruff check/format pass, and Git whitespace check passes. Both staged helper copies now include
the suspended apply/route-repair CLI and bounded original command stderr. Tests verify refusal
before any trial operation; these results do not assert that the managed recovery works on macOS.
The next diagnostic is the actual root `ifconfig bridge0 addm en5` stderr on A, now that the existing
service has raised the bridge. This targets restoration of the original membership, not another
address/route cutover. Hold B's trial commands until A's recovery is understood and verified.

## Confirmed BRDGADD failure and approved cold recovery (2026-10-04)

The user supplied the missing OS error: `ifconfig: BRDGADD en5: Operation not supported on
socket`. Raw reattachment is rejected, including after the existing service raised the bridge.
Stopped that recovery path. Read-only checks confirm the original saved addresses, service
settings and full bridge-member lists are intact on both Studios. Presence of bridge SPI symbols
is not proof of a working alternative; Apple's published BridgeConfiguration implementation also
issues BRDGADD, so no unverified private-framework mutation was attempted.

The user explicitly authorized a controlled restart of **A**, accepting interruption of A's
workloads. The authenticated control SSH request used the existing authorized macOS user session:
`osascript -e 'tell application "System Events" to restart'`. It returned successfully. Startup
then refused public-key SSH with macOS's locked-system banner; the user unlocked A locally.
No account password was read, requested in chat, stored or printed.

Post-unlock measurements on A:

- Boot time: **2026-10-04 10:06:10**, confirming the full restart.
- `bridge0` is UP/RUNNING/active with the original `192.168.100.11/24` address.
- `en5` is again an original bridge member and has no direct IPv4 address.
- Peer route is the original subnet route through `bridge0`, not the Wi-Fi default or the
  trial static peer/self-MAC route. RDMA remains enabled.
- Authenticated `/node/health`: **200, path=control**. Both data links still report down
  while B retains its incomplete trial state. No end-to-end recovery is claimed yet.

After this verified local recovery, the user separately authorized B's controlled restart.
The same authenticated System Events restart request returned successfully. B subsequently
became unreachable over SSH as expected during startup. Await B's startup unlock, then verify
original bridge/address/routes, authenticated node/data-link status and both-direction peer
reachability before declaring the data network restored. Direct-interface trials remain suspended.

### Both-node cold rollback completed

The user confirmed B's startup unlock. Post-boot observations confirm boot time
**2026-10-04 10:14:53**; B's original `192.168.100.12/24` address is back on active `bridge0`,
`en5` is a bridge member without a direct IPv4 address, the peer subnet route uses `bridge0`,
and RDMA remains enabled. Peer SSH succeeds in **both directions** and A's scoped self-SSH
enrollment still works. Three A-to-B ping samples have **0% loss**, min/average/max RTT
**0.400/0.621/0.757 ms**.

Authenticated final `/node/health`: **200, path=control** on both. Authenticated
`/node/data-link`: **200, ip_state=up** on both. The RDMA projection remains degraded because
there is no verified current collective evidence; this does not claim RDMA training acceptance.
The original IP/control data network is restored after the incident. No further direct-interface
trial is authorized as a verified procedure; its CLI remains suspended. Continue implementation
with dataset foundations while a future durable RDMA setup remains an explicit unpassed gate.

## Dataset foundation resumed after recovery (2026-10-04)

- Added `coire_api/training/storage.py` for private generated-UUID source staging, streamed byte/
  row bounds, bounded oversized-line buffering, strict UTF-8/JSON row checking and complete
  canonical normalization. Invalid rows are counted completely with capped content-free field
  diagnostics. Duplicate JSON keys, nonfinite values, image-bearing rows and empty input refuse
  a valid split. Metadata does not alter duplicate groups; exact groups stay in one partition.
- Source and split metadata publish together by fsynced directory rename after checksum
  re-verification. Linked stores, changed staged bytes, overwrite attempts and interrupted
  uploads are refused/cleaned. No tokenization, model load or tensor work runs on core.
- Added `coire_api/training/quota.py` for transactionally counted **pre-body** upload/spool/source/
  index holds. This primitive must be wired before multipart parsing in the upcoming route.
  Real disposable-Postgres concurrent admissions prove only one competing hold fits a one-hold
  quota. Uncertain cleanup remains counted; confirmed staging cleanup cannot free an already
  retained immutable source, which requires its separate future deletion proof.
- New focused storage and quota tests: **18 passed, no skips**, including **3 real-Postgres**
  quota/cleanup cases. Combined core/schema/data-format/storage/quota/network-script selection:
  **395 passed, no skips**. Full Ruff check/format (1,389 files), full strict mypy (698 files),
  generated OpenAPI freshness and Git whitespace checks pass.
- These are foundation primitives, not completed upload/analysis routes. T026–T028 remain
  unchecked until route auth/origin/audit, durable analysis/grants and all mapped contract tests
  are implemented and verified. Feature exposure remains default-off; no new production route
  or Studio runtime deployment is implied. No feature-016 completion is claimed.

## Shared integration and regression verification (2026-10-05)

Continued on `feat/016-sft-training-jobs`, preserving the existing uncommitted implementation.
Requirements checklist remains 16/16; no extension hooks exist. The implementation loop is inactive.

- Registered training and adapter admin routers in the actual API application, then regenerated
  API OpenAPI and web TypeScript through the documented generators. Existing base/client regressions
  pass alongside the new schemas. T031 is verified for registration/schema compatibility only.
- Fixed cold-engine transport: placement parses the persisted `InferenceTarget` and sends it through
  `NodeClient.start_engine`; the client refuses missing/different exact-target acknowledgment, and
  placement rechecks identity after readiness polling before persisting a ready engine. Three new
  transport cases reproduced the missing keyword/transport before the fix, then passed.
- Added shared wire validation for evaluation targets and run-container commands: model/variant must
  match the target, selector parent must match the model, and adapter target/pair selector must appear
  together. Twelve cross-contract inconsistency cases pass; node-local defensive validation remains.
- Repaired the VLM re-adoption fixture to provide the owned process's exact argv to the strengthened
  ownership check. No production ownership check was loosened. Node unit/contract suite:
  **571 passed, 1 existing platform skip**.
- Fixed web consumers of generated optional defaults with explicit schedule/evaluation/placement
  defaults and bounded empty-list handling. Production web build, lint and all **167 web tests** pass.
- Training disable now leaves authenticated history/progress/checkpoint/profile reads and pause,
  cancel and terminal deletion available; resume, new submission/validation and measurements remain
  gated. Three route tests confirm pause/cancel reach domain handling while disabled resume refuses.
- Updated the cold-stream cancellation fixture for the optional exact-variant field and regenerated
  failover OpenAPI after its shared selector schema changed. Neither fix weakens an assertion.

### Commands and measured results

- `uv run --frozen mypy apps/ packages/`: **745 files pass**.
- `uv run --frozen ruff check .`: pass; `uv run --frozen ruff format --check .`:
  **1,439 files already formatted**.
- `uv run --frozen pytest -q`: initial **3 failures, 2,358 passed, 266 skipped** revealed the two
  stale cold-stream fixtures and failover schema; after fixes, **2,364 passed, 266 skipped** in
  118.20 s. Skips are not training, integration or hardware acceptance evidence.
- `COIRE_INTEGRATION=1 uv run --frozen pytest -q apps/coire-api/tests/contract/test_training_api.py
  apps/coire-api/tests/contract/test_adapter_targets.py
  apps/coire-api/tests/contract/test_training_datasets.py tests/integration/test_training_targets.py`:
  first run exceeded 120 s without a reported test failure; rerun with a 300 s limit completed
  **77 passed, no skips** in 123.33 s using disposable PostgreSQL fixtures.
- `pnpm -C apps/coire-web build`, `pnpm -C apps/coire-web test`,
  `pnpm -C apps/coire-web lint`: pass (**42 test files, 167 tests**).
- API and failover generated OpenAPI `--check`: pass. `git diff --check`: pass.

### Remaining integration and acceptance

The inspected node training router still exposes analysis commands only; native training attempt
routes, shared admission accounting and lifecycle registration are unfinished. Scheduler workers
register analysis but no training command executor. Reserved measurement execution still refuses
new experiments, and extraction transport references shared extraction contracts that do not yet
exist. These are implementation gaps, not evidence of successful end-to-end training.

Next: complete those typed native preparation/input/extraction and scheduler seams, then the full
recipe/form-to-served-adapter acceptance, mixtures, physical checkpoint recovery, distributed
training and coexistence gates. Current JACCL collective evidence remains unavailable; the restored
bridge network is preserved and the suspended repair helper was not run. No model/Metal workloads
ran on core and no Studio build was deployed in this session. No commits, pushes or PRs were made.

## Continued implementation and real worker-to-serving gate (2026-10-05)

Resumed under the user's request to continue until full build and test acceptance. The branch is
`feat/016-sft-training-jobs`; the requirements checklist passes 16/16 and no extension hooks exist.
The implementation controller remains inactive. The previous "Remaining integration" description
is superseded for native attempt routes, scheduler registration, shared extraction contracts and
measurement execution: these are now wired and exercised by the expanded current-source suites.

### Corrections and cleanup implementation

- Corrected two renamed runtime-test imports and strict annotations in test doubles, secret values,
  generated fixture shapes and enum/SQL timestamp projections. No production validation/type gate
  was loosened. Current full strict mypy covers **789 files**.
- Reproduced the disabled dataset-worker failure before changing code. Scheduler now always owns
  the dataset analysis lane; disable prevents dispatch inside the reducer while cancellation,
  uncertainty reconciliation, retired dataset purge and upload cleanup keep running. Worker stop/
  restart is idempotent. Actual PostgreSQL tests cover both revocation and disable during analysis,
  retaining held memory until native cancellation/cleanup is proved.
- Implemented node artifact DELETE with exact immutable request journaling, fsynced retirement
  before namespace removal, private hidden purge, restart replay, symlink/unlisted-file refusal,
  live grant refusal and local trainer/engine/import/extraction reference checks under the shared
  admission lock. Retired artifacts cannot be imported/published again. The authenticated
  control-only route remains available when training is disabled and emits spans/logs/metrics.
- Added a scheduler-owned retention lane and exact-copy authenticated transport. Job locks fence
  cleanup with resume/promotion; pending/dispatching commands survive lost acknowledgements. Core
  records checkpoint purge only after both copy receipts prove it, preserving metadata/lineage.
  Recovery rewind tests additionally protect the actual current lower-update checkpoint, not just
  the highest historical update. Local deletion currently waits conservatively for native trainer
  release. Attempt scratch/input/rank-component purge and whole-envelope release are still
  incomplete; a copy purge does not release those uncertain disk holds. T077–T078 remain unchecked.
- Updated two old regression doubles for the actual reverse-admission queries/locks and
  regenerated OpenAPI/web types for current schema changes. Their original refusal assertions pass.

### Measured development checks

- Initial expanded integration selection failed collection on the two stale imports; after repair:
  **208 passed, no skips**. After retention and disabled-analysis tests, the same selection plus
  new tests (`COIRE_INTEGRATION=1 uv run --frozen pytest -q apps/coire-api/tests/integration
  apps/coire-api/tests/contract/test_training_api.py
  apps/coire-api/tests/contract/test_training_datasets.py
  apps/coire-api/tests/contract/test_adapter_targets.py tests/integration/test_training_targets.py
  tests/integration/test_training_distributed.py`): **212 passed, no skips**, 230.85 seconds.
- Initial broad regression exposed two stale OpenAPI assertions and two incomplete query doubles.
  After regeneration/fixture corrections, `uv run --frozen pytest -q`: **2,560 passed, 331 skipped**,
  166.72 seconds. Required hardware/integration checks are run separately; skips are not acceptance.
- `uv run --frozen mypy apps/ packages/`: **789 files pass**. Full Ruff check passes;
  full format check: **1,495 files formatted**. API OpenAPI freshness and `git diff --check` pass.
- `pnpm -C apps/coire-web test`, `lint`, `build`: **167 tests / 42 files**, lint and production
  TypeScript/Vite build pass. Generated types were refreshed after that web run.

### Real Studio A numerical, process, extraction and serving

- Authenticated control SSH confirms `coire-edge-a.lab` and installed MLX **0.32.2** / mlx-lm
  **0.31.3**. Staged source under the existing `~/coire-stage/016-runtime-gate`; isolated test
  dependencies under `~/coire-stage/016-test-deps` include locked pytest **9.1.1** and pytest-asyncio
  **1.4.0**. No installed node/environment, acquisition bytes or production config was replaced.
- Numerical fixture: existing admin-acquired local
  `/opt/coire/models/mlx-community--Qwen2.5-Coder-0.5B-Instruct-4bit.mcp-acceptance`, verified against
  its adjacent acquisition manifest before loading. No download occurred; no model/Metal ran on core.
- Enabled offline selection of `test_training_{runtime,resume,rendering,worker,cpu_analysis}.py`
  with the versioned Studio interpreter and `-o asyncio_mode=auto`: **18 passed, zero skipped**,
  26.70 seconds before the added serving assertion. Source-only staging initially lacked the
  async test plugin and the worker test passed an obsolete extra `collective` field; both were
  corrected. The test-only native process adapter supplies only the two staged package roots so
  the real worker imports the tested source. Production environment filtering remains unchanged.
- Six trajectory comparisons (three single-source, three mixture-backed) complete 36 updates,
  interrupt after update 4 and resume for 32 with dropout 0.15, accumulation 2 and changing
  learning rate. Exact sample/RNG/serialized state checks and fixed adapter/optimizer/loss
  tolerances all pass, including negative reset controls. T091 is verified.
- Extended the native CLI gate beyond checkpoints: CPU analysis -> an owned native trainer ->
  two locally verified checkpoint replicas -> genuine checkpoint acknowledgement -> node
  extraction -> dedicated bare `mlx_lm.server` -> authenticated node proxy generation. The
  engine readiness and final request prove the exact adapter target. The test closes its own
  engine/control client and never stops unrelated workloads. Revised worker selection:
  **3 passed, zero skipped**, 10.00 seconds. Extraction's actual synchronous 200 response is
  asserted; no endpoint/validator was changed to accommodate the test.

T026–T028 and the T032 offline compatibility gate are now checked. This is genuine single-Studio
QLoRA inference and local-second-store evidence, not physical peer replication, full admin/UI
recipe acceptance, dense LoRA/DoRA, Studio-reboot or two-rank/coexistence acceptance.
Read-only Studio A inspection still finds **no `/opt/coire/state/jaccl-hostfile.json`**. The restored
bridge fabric is preserved; the suspended direct-interface trial was not run. Feature completion
and release tasks remain unchecked, and all work remains uncommitted.

### Final gate refresh, bridge generation and concrete RDMA prerequisite

- Final refreshed Studio selection including actual extraction/serving:
  **18 passed, zero skipped**, 33.53 seconds. Registered the `engine` marker explicitly in the
  isolated command; only the intentionally absent test secret-directory warning remains.
- Corrected checkpoint cleanup projection to a typed additive **`purging`** state while copy
  erasure is unresolved. A checkpoint with a removed copy cannot continue claiming committed
  two-copy readiness. The real Postgres test checks the projected state after the first receipt.
  Contract/data-model compatibility notes and generated API/TS types are updated in the same change.
- Initial current JACCL generation reproduced the old helper's direct-interface-cutover refusal.
  Added and tested a read-only `--generate-on-bridge` mode: native discovery still owns RDMA device
  fields; existing IP bridge endpoints/members/routes are validated, never changed. Native MLX
  successfully generated `~/coire-stage/016-jaccl-hostfile.json` on Studio A.
- An authenticated isolated coire-node probe exposed the existing incorrect `python -m mlx.launch`
  argv; MLX 0.32.2 provides `mlx._distributed_utils.launch:main`, not that module. Probes, sharded
  serving and benchmarks now call that fixed bare entry point with their versioned interpreter.
  Existing argv/parser tests and a new bounded/private failure-diagnostic regression pass.
- The next probe exposed native launcher's `ssh -tt` requirement: direct self-SSH failed with
  `PTY allocation request failed on channel 0`. The user explicitly approved **only PTY** on Studio
  A's existing source-address-scoped restricted self-key. Applied after exact public-key fingerprint,
  one-entry, address-scope, private-file and concurrent-content checks, with a private immutable
  backup. Self-SSH with `-tt` then succeeds. No key replacement, forwarding allowance or host-trust
  weakening occurred; the helper and backup remain outside Git.
- The subsequent real probe reached the native RDMA runtime and failed with:
  **`[jaccl] No IPv4-mapped GID for this device. Thunderbolt RDMA ports only publish one once the
  interface has an IPv4 address`**. This is measured runtime failure, not a missing-permission guess.
  Probe returns typed failed outcome; native launcher's exit 0 does not count as success without
  both rank result records. The isolated probe has no model/user prompt or production config changes.
- User approved preparation of a **reversible alias-only** setup preserving the restored bridge.
  Added `deploy/cluster/scripts/studio-rdma-alias.py`, with immutable private baseline, mapped-GID
  verification, delayed unchanged-route/membership checks and removal of only the generated alias.
  Combined alias/bridge/historical helper tests: **52 passed**; focused strict mypy passes. The
  original direct-interface apply/repair remains suspended.
- Staged the prepared alias helper on both Studios at
  `/Users/mcteer/coire-stage/studio-rdma-alias.py`. Real read-only `--check --interface en5` succeeds
  on both, reporting aliases **169.254.102.217** / **169.254.102.218**, mask **255.255.255.252**,
  bridge preserved and root required. No alias was applied. Actual `sudo -n true` on both refuses
  **`sudo: a password is required`**. Preparation approval does not supply operator/root credentials.

**Concrete next prerequisite:** operator-authenticated application of the prepared alias-only trial
on both Studios, then actual mapped-GID, peer connectivity and native collective verification. The
exact apply/rollback commands are in `docs/runbooks/sharded-serving.md`; do not repeat the failed
bridge-removal commands. Do not declare two-rank training, physical checkpoint/reboot acceptance,
coexistence or feature completion passed. Remaining software/release tasks, including complete
attempt scratch/component cleanup and the full acceptance driver, are also still unchecked.

Final checks after the cleanup enum, launcher and prepared-network changes:

- `uv run --frozen pytest -q`: **2,573 passed, 331 skipped**, 156.93 seconds; no failures.
- Strict workspace mypy: **790 files pass**. Full Ruff check and format pass (**1,499 files**).
  Generated API OpenAPI freshness and Git whitespace checks pass.
- Enabled real-Postgres retention/controller/runtime/checkpoint transaction regression:
  **48 passed, zero skips**, 43.66 seconds, including readable `purging` checkpoint projection.
- Refreshed web test/lint/build after type regeneration: **167 tests / 42 files**, all pass.

Implementation is paused for the concrete operator/root prerequisite above; it is not declared
finished. Alias application, actual collective success and remaining feature/release verification
are not implied by the green development suites. No commits, pushes or PRs were made.

### Operator applied Studio A alias

The user reported successful root application on Studio A. Independent read-only control-SSH
verification confirms alias **169.254.102.217**, its actual IPv4-mapped RDMA GID, the original
**192.168.100.11/24** on `bridge0`, peer data routing through `bridge0` and core control routing
through `en1`. Studio A's alias/GID prerequisite is resolved; this is not yet collective acceptance.
Studio B's actual `ibv_devinfo -v -d rdma_en5` still reports `PORT_ACTIVE` with only non-IPv4-mapped
GIDs. Operator-authenticated application on B remains the next prerequisite before repeating JACCL.

### Both aliases verified; real JACCL runtime gate passed

The user reported successful alias/GID application on Studio B. Independent read-only control-SSH
verification confirms **169.254.102.218** with its mapped GID, original **192.168.100.12/24** on
`bridge0`, data routing through `bridge0` and core routing through `en1`. No address/member/route
cutover was performed. The operator/root prerequisite above is resolved.

Three consecutive authenticated isolated coire-node `POST /node/link-probes` trials using the
native-generated unchanged-bridge hostfile and pinned bare launcher succeeded with both rank
records (MLX 0.32.2; Studio A macOS 27.0, Studio B macOS 27.0.1):

| Observation ID | Bandwidth bytes/s | Latency ms |
| --- | ---: | ---: |
| `a6251c55-4bee-4f77-b4f8-0bfdbf1b9df3` | 9,258,523,855 | 0.161333 |
| `d87ccd9a-5e4f-48bc-aa45-3822a105ed2e` | 9,258,204,550 | 0.158959 |
| `d02ddeb1-fdec-418f-a77c-5d67036dbee7` | 9,258,683,526 | 0.168000 |

These are real native collective results, not simulated measurements. They resolve the runtime
RDMA feasibility blocker. The isolated node stores/results are outside Git; existing production
node configuration/control-plane link rows were not changed by these trials. Generated-hostfile
deployment, reboot-persistent alias recovery and full two-rank training/fault/coexistence acceptance
remain distinct uncompleted gates. Normal implementation resumes under the user's standing request.

### Built-wheel gates, matrix acquisition and further implementation

- Built the core/node distributions and selected **87 locked macOS arm64 dependency wheels**
  through `scripts/build-node-wheel.sh`, preserving the existing verified wheel cache. Staged
  them on both Studios. Fresh isolated installed environments under `~/coire-stage/016-built-env`
  pass `uv pip check` for **89 packages** and the repository's pinned native import/CLI smoke.
  No active node/service was changed by those isolated builds.
- Enabled runtime/resume/rendering/worker/CPU-analysis selection from **installed wheels**, with
  only separate test dependencies on PYTHONPATH: **18 passed on A (37.90 s)** and **18 on B
  (40.44 s)**, zero skips. Tests bind the actual Studio identity. A further built-environment
  authenticated JACCL result (`0ef7c723-a984-4443-b1b2-12f0398d8bbe`) succeeds at
  **9,261,612,196 bytes/s / 0.162208 ms**.
- Added installer `--stage-only` and a reusable immutable candidate verification step. The
  new tests first failed for absent staging implementation; all **15 installer tests** now pass,
  with strict mypy/Ruff and shell syntax checks. This avoids flipping an active environment before
  its controlled service activation. Initial identical candidates
  `/opt/coire/envs/0.2.0-341224d8d983` are independently staged/smoked on both; active links/plists
  remain unmodified.
- Built distinct linux/arm64 API and scheduler images as `coire-{api,scheduler}:016-acceptance`.
  Both pass all seven repository image-policy rules, including no shell/package manager, non-root,
  read-only compatibility, pinned bases and absence of the user harness on core. Independent
  network-none/read-only/cap-drop import probes validate real API training routes/three packaged
  recipes and scheduler training/analysis registration. Scans/SBOM and other image gates remain
  pending; successful builds/policy probes alone do not check T121.
- Added `scripts/validate-sft-training.py`: typed authenticated recipe/replay/checkpoint/loss/
  adapter/gateway checks, bounded pause/resume/cancel trials, private external metadata reports,
  Keychain-backed credentials and CI Studio refusal. Five synthetic-transport tests pass,
  covering all three control paths, exact selectors/variants, no prompt/recipe/credential output,
  independent-verification refusal and refusal to pass an unperformed cancel. Strict mypy and
  Ruff pass; CLI help runs. T108 is checked for the driver, not execution of remaining real trials.
- Read-only credential metadata identified the existing `coire-015-mixed-benchmark-key`; its
  authenticated admin registry read returns 200. Legacy admin is 401; the development key is 429.
  Credentials remain inside the process and never enter files/logs/reports.
- Through the audited admin variant endpoint, submitted immutable
  `016-dense-bf16` intent (`sft-016-dense-bf16-v1`) for existing model
  `7d3107b0-56c8-4b1e-8af4-c1f3e50be0db`. Workflow
  **`ec984170-c473-4408-9a5c-45cd62528b0a`** explicitly selected an already-verified copy for
  dequantization and **skipped external pull**; conversion/validation/replication succeeded.
  Dense variant **`ce1470c6-14c9-457e-b4c7-2be6efbcd7bc`** is stored on both as
  `mlx-community--Qwen2.5-Coder-0.5B-Instruct-4bit.016-dense-bf16`. Existing published/default
  variant remains `cf3c3ddc-b205-40bb-8374-1a02ec91693e` (`mcp-acceptance`).
- Added an explicit numerical fixture parameterization selection without changing accepted
  runtime options. Dense LoRA on A: **18 passed, 54.10 s**. Dense DoRA on B: **18 passed,
  56.12 s**. Dense DoRA on A: **18 passed, 52.92 s**. Each includes three single-source plus
  three mixture interruption comparisons, real full-state checkpoints and actual extracted
  adapter generation; DoRA additionally proves its magnitude tensors are trainable. No skips,
  downloads, base edits or acceptance-tolerance changes occurred.
- The first LoRA/B run produced **17 passed, 1 failed**: after update 4 and both commits, native
  liveness became unknown at child exit. Independent inspection found no remaining trial group.
  Reproduced the owned-zombie/argv-clear race in new CPU tests, then fixed observation to require
  matching birth/session plus positive owned-Popen waitpid proof before group absence can prove
  death. Live argv mismatches/PID reuse and remaining group members still retain unknown holds.
  **59 focused native ownership/lifecycle tests pass**, with strict mypy/Ruff. The identified
  old isolated trial was reconciled through its supervisor with exact PID/create-time scope and
  a new stop command; actual death was proved before its memory hold was released.
- Rebuilt locked wheels and staged the corrected immutable candidate
  **`/opt/coire/envs/0.2.0-04f3a9424fb1`** on B without activation. Full LoRA/B wheel-based gate
  then completes **18 passed, zero skips, 53.24 s**. This corrects the failed gate without
  accepting unknown as running, resetting state, or disabling an assertion. T060 is now checked
  for all approved parameterizations producing genuine served adapters. Physical two-rank
  training/reboot/core-admin-form and coexistence scenarios remain unchecked.

### Training Jobs projection and refreshed regression

Completed T062 using a focused console training projection module and the already-mounted
Training activity section in the existing Jobs page. The new typed bounded endpoint requires
current human-admin authority, supports disabled-mode history, excludes recipes/data/paths, and
shows latest non-rolled-back losses plus all counted held/releasing attempt memory. Stable opaque
timestamp/ULID pagination does not change existing UUID activity contracts. Confirmed Stop uses
the same audited training cancel endpoint with the projected version and generated request types;
it preserves visible history when a version conflict occurs.

- Real PostgreSQL tests cover rolled-back loss exclusion, uncertain memory retention, release,
  cancelling stop availability, service-authority refusal, invalid cursors, tied-ULID paging and
  retired-row exclusion. New activity/retention/contract selection: **7 passed, zero skips**.
- Two meaningful UI cases cover confirmation/versioned cancellation, live reservation/loss view,
  conflict visibility and older-page retrieval. Existing image/instance Stop assertions remain.
- Refreshed Python regression: **2,584 passed, 334 skipped**, 146.71 seconds. Integration/hardware
  gates remain separately enabled and are not counted from those skips.
- Full strict workspace mypy: **793 files pass**; focused driver/installer type checks pass. Ruff
  check and format pass (**1,504 files**). Generated OpenAPI and web types were refreshed together.
- Web test/lint/build: **169 tests / 43 files**, all pass. The new activity card uses generated
  shapes and the shared API client; no component fetch or raw source YAML is needed.

Full feature completion is still pending; neither the runtime matrix nor this projection closes
the independent real admin/form, physical checkpoint/reboot, two-rank training, coexistence,
complete scratch/component retention, scan/SBOM or rollback gates.

The earlier API acceptance image (`sha256:d312ed8d8340dadf5c4310f5781c8933439ee613278015fe96486a05d52afce8`)
also passes a current Trivy 0.74.0 CRITICAL vulnerability scan (Debian 13.7, Python packages and
healthcheck binary: **0 critical findings**). This scan identifies the earlier built image;
the later console projection change requires a refreshed image build/scan before release.
No vulnerability gate or database-update check was skipped, and T121 remains unchecked.

## Continuous implementation resumed

The user enabled the session controller and invoked implementation. Controller is active and bound
to `/Users/mcteer/Projects/coire/specs/016-sft-training-jobs/tasks.md`; requirements remain 16/16 and
no extension hooks exist. Work continues without declaring feature completion from component gates.

- Corrected live artifact-reference tracking so obsolete committed checkpoints can be retired
  while training continues, with current staged/committed/resume points protected. Older live
  journals remain conservatively protected. Rank-component grants/imports join the reference check;
  component control and memory/journal operations use the shared admission lock.
- Added strict retirement-only attempt cleanup contracts and authenticated disabled-mode control
  route. Fresh whole-group death and released memory proof precede exact durable cleanup intent;
  failed purge remains hidden/countable and exact replay resumes it. Serving artifacts are retained.
- Connected core cleanup commands/receipt reduction to the scheduler retirement lane. Only retired
  terminal jobs with all checkpoint erasures and participant stop proofs can dispatch cleanup;
  exact scope-matched native erasure proof releases the participant disk hold.
- Added authoritative-manifest erasure for missing/partial/corrupted unreferenced artifacts, complete
  checkpoint rank-component removal and owned import cancellation. Corruption cannot masquerade as
  readiness or prevent an otherwise authorized contained erasure. Unknown/link/unlisted references
  remain protected. Fixed the new unit fixture's cross-directory import so focused selections work.
- Focused native ownership/accounting/component/lifecycle/retirement runs pass: **77**, then **79**
  tests; enabled real-Postgres controller/checkpoint/retirement selection: **34 passed**, no skips.
  Full strict workspace mypy: **794 files pass**. Shared contracts/notes and OpenAPI/TS are refreshed.

### Privileged service-control prerequisite

Integrated acceptance must activate compatible native node handlers on the existing authenticated
control/data listeners; isolated app/numerical gates do not replace that service activation. Both
real `sudo -n /bin/launchctl print system/com.coire.node` checks fail with `a password is required`.
An attempted standard macOS administrator-authorized read-only service check on A also fails with
authorization error **-60007**. No sudo policy, service/plist, active environment or primary listener
was changed by these checks. A protected operator root session is required for the controlled node
service activation/restart stage. This is a concrete credential prerequisite, not task completion.

## Session 2026-10-06: Studio A activation and retention replay verification

The operator reported successful activation of `/opt/coire/envs/0.2.0-0c464ee1c692`
on A, with retained rollback backup `/opt/coire/state/node-activation-m_hu22l0`.
Independent Keychain-authenticated checks through the declared control endpoints confirm:

| Node | GET /node/health | POST /node/training/reconcile |
| --- | ---: | ---: |
| coire-edge-a | 200, control | 200 |
| coire-edge-b | 200, control | 404 |

B's staged activation helper/candidate passes its non-mutating dry run and reports preserved
installed settings. Operator-authenticated activation on B is the concrete next deployment
prerequisite for physical peer checkpoint replication and integrated training acceptance.
A's prior root-activation prerequisite is resolved; no additional A activation is requested.
The operator's successful activation also executes the helper's core lease compatibility preflight;
this session did not independently reapply or inspect production migration 0031.

### Retention and wiring checks

- Confirmed `SchedulerWorkers` owns `TrainingRuntimeWorker`, whose startup owns controller,
  rank-component, retention and baseline metrics lanes, including reconciliation while disabled.
- Added a real-Postgres scan-to-dispatch-to-receipt test for retired attempt workspace cleanup.
  A lost native erasure acknowledgment leaves the durable command `dispatching` and disk hold
  `held`; a fresh worker replays the exact same request, then releases only the scoped hold
  after positive erasure proof. Historical job metadata remains present.
- Reproduced a deletion-replay failure with an inert 32-file authoritative manifest: native
  erasure succeeded, but the resulting retirement journal exceeded the old 4 KiB reader ceiling
  and exact replay failed. Journals embed manifests plus file inventories; the reader now uses
  a bounded 128 MiB envelope corresponding to the existing 64 MiB manifest ceiling and refuses
  nonprivate files as well as linked/nonregular content. Exact post-restart replay now passes.
- This source correction is **not included** in activated/staged candidate
  `0.2.0-0c464ee1c692`; a refreshed frozen node build remains required before release verification.

### Measured commands/results

- Initial enabled retention/component/lifecycle/activation selection: **79 passed, no skips**.
- `COIRE_INTEGRATION=1 uv run --frozen pytest -q apps/coire-api/tests/integration
  apps/coire-api/tests/contract/test_training_api.py
  apps/coire-api/tests/contract/test_training_datasets.py
  apps/coire-api/tests/contract/test_adapter_targets.py tests/integration/test_training_targets.py
  tests/integration/test_training_distributed.py apps/coire-node/tests/unit/test_training_retention.py
  apps/coire-node/tests/contract/test_training_components.py
  apps/coire-node/tests/contract/test_training_lifecycle.py tests/test_training_node_activation.py`:
  **292 passed, no skips**, 234.83 s, after the native replay correction.
- After the added scheduler lost-ack regression, enabled focused retention suites:
  **13 passed, no skips**, 6.32 s.
- `uv run --frozen pytest -q`: **2,590 passed, 336 skipped**, 152.04 s. Unenabled
  integration/engine/platform skips do not count as hardware acceptance.
- Full strict mypy: **794 files pass**; subsequent focused strict check covers all three
  changed source/test files. Full Ruff check/format (**1,508 files**), generated API OpenAPI
  freshness and `git diff --check` pass.

The requirements checklist remains 16/16; no extension hooks exist and the session loop is
disabled. All changes remain uncommitted. Full recipe/form-to-adapter, physical peer/reboot,
two-rank/fault/coexistence and refreshed packaging/release gates remain uncompleted. T077/T078
are not checked from these narrower retention replay results.

## Studio B activation and integrated ingestion (2026-10-06)

The operator activated B's `0.2.0-0c464ee1c692` candidate and retained backup
`/opt/coire/state/node-activation-3b0hac53`. Independent Keychain-authenticated control
checks now return **200** for health and training reconciliation on **both Studios**.
The earlier B route/activation prerequisite is resolved.

Temporarily enabled only API/scheduler training acceptance via a narrow external Compose
override of the installed release configuration. No model/Metal workload ran on core.
Through the authenticated audited admin routes:

- Uploaded 64 project-generated prompt/completion arithmetic rows with provenance,
  split seed 42 and validation fraction 0.125. Upload returned **202**; genuine Studio CPU
  analysis completed and dataset **`2754b09b-06e5-5fc0-8c3c-3d7b1f211666`** became **ready**,
  analysis **`c8bb8b8a-243c-4b3b-bcd0-dba69f84e6c6`**.
- Bound the packaged QLoRA recipe to the existing acquired Qwen2.5-Coder-0.5B model/variant
  (`7d3107b0-56c8-4b1e-8af4-c1f3e50be0db` /
  `cf3c3ddc-b205-40bb-8374-1a02ec91693e`): rank 2, scale 2, 12 updates, sequence 128,
  checkpoints/evaluation every 4 updates, single A. The private recipe is outside Git.
  Validation returned **200**, unresolved with **profile_missing**, as required before
  measured resource evidence. No caller estimate was substituted.
- Submitted isolated native memory measurement
  **`3f6f0a87-18eb-4988-83a9-ac19fb71fc71`** (**202**). It remained queued through the
  bounded 180-second observation. Read-only persisted admission diagnosis found healthy/fresh
  ledgers and no model/training/image/conversion holds, but **thermal_state=unknown on both**.
  The probe has **no persisted dispatch** and did not start a trainer.

### Actual thermal-source defect and corrected candidate

The existing ioreg reader expects IOPMrootDomain's `ThermalPressureLevel`; the deployed
macOS builds do not supply that field. The actual public `NSProcessInfo.thermalState`
API returns **0 (nominal)**. Added a public-native fallback with distinct explicit object
and NSInteger `objc_msgSend` signatures, no new dependency or privileged helper. Missing,
unrecognized and load-failed readings remain unknown. Existing IOKit values remain mapped
unchanged. Red tests preceded the implementation; no admission/guard threshold was loosened.
Plan and runbook document this source refinement and queued-probe diagnosis.

- Focused metrics/retention: **30 passed, no skips**.
- `uv run --frozen pytest -q apps/coire-node/tests/unit apps/coire-node/tests/contract`:
  **697 passed, 1 existing platform skip**, 39.72 s.
- Full strict mypy: **794 files pass**. Full Ruff check/format (**1,508 files**) and
  Git whitespace check pass.
- Rebuilt core/node wheels and verified **87 locked dependency wheels**, then staged
  immutable **`/opt/coire/envs/0.2.0-a306261daee6`** on **both Studios** through the installer
  `--stage-only` path. Both pass 89-package dependency compatibility and native import/API smoke.
  Actual installed-wheel `_read_process_thermal_level()` / `read_thermal_state()` return
  **0 / nominal on both**. This candidate also includes the preceding retention-journal replay fix.
  The intermediate retention-only `0.2.0-3d5722a4a0d2` is superseded; do not activate it.

Restored API/scheduler acceptance configuration to **TRAINING_ENABLED=false** while awaiting
corrected node activation. Independently checked the running scheduler flag is false and the
measurement remains **queued, no dispatch**. Dataset metadata/source and audited measurement
intent remain preserved. No training completion or adapter output is claimed.

**Concrete next prerequisite:** activate corrected candidate `0.2.0-a306261daee6` on A and B
with the already-staged service-preserving activation helper. Fresh `sudo -n true` checks on
both still refuse **a password is required**; agent SSH cannot restart the system LaunchDaemon.
After operator activation, independently verify fresh nominal thermal health, re-enable bounded
acceptance and continue the existing queued measurement before recipe-to-adapter/recovery trials.
All feature/release acceptance gates remain open where their required evidence is missing.

## Both corrected activations verified; native isolation/rejection findings (2026-10-06)

The operator activated corrected candidate `0.2.0-a306261daee6` on both Studios:

- A backup: `/opt/coire/state/node-activation-l0qhlb1h`.
- B backup: `/opt/coire/state/node-activation-01fp5dyi`.

Independent authenticated control health returns **200, thermal_state=nominal** on both,
with fresh observations at 14:50:28/14:50:33 UTC. Authenticated training reconciliation
returns **200** on both. The corrected activation/thermal prerequisite is resolved.

After A's verified activation, temporarily re-enabled bounded API/scheduler acceptance.
The existing single-A memory measurement moved to **running**, with persisted dispatch for
attempt **`01M48TY93W782R7QSFB23VDBVV`**, job scope **`01M48TY93WGBFW2G4WTC1AAKZT`**.
It did not complete through the bounded 150-second observation. Scheduler logs repeatedly
report inconclusive execution; authenticated native measurement status returns **409**
(`training command or ownership proof unavailable`). That response does not establish
successful native preparation, training, or stop proof. Core still retains the dispatch/hold.

### Identified leftover test engines

Read-only process inspection and authenticated node engine inventory identify five orphan
bare `mlx_lm.server` processes on A. Their exact `--adapter-path` values point to the earlier
feature-016 `test_offline_native_cli_full_f0` pytest directories (runs 6 through 10), not
production adapter paths. Explicitly scoped cleanup through audited
`DELETE /api/v1/admin/engines/{engine_id}` returned **202** for each:

| Engine UUID | Observed PID | Final authenticated state |
| --- | ---: | --- |
| `6a138a06-7dfe-4733-a9fb-1c8c499b0a20` | 21600 | stopped |
| `fb996e76-0cfb-4d8e-a121-15c1e970b99f` | 21672 | stopped |
| `8948e89b-8553-4d97-961b-a68cba5fce2d` | 22823 | stopped |
| `48b3fa7d-a993-4523-ba1a-76f5a5519dc2` | 23235 | stopped |
| `e25d4464-e311-4f6c-b54e-629098293acc` | 23304 | stopped |

No raw PID kill or unrelated model stop was used. This removes the identified test-engine
occupancy; it does not prove every process on the node is observable or the probe is accepted.

### Remaining concrete findings

The installed LaunchDaemon runs as `mcteer`. Read-only process inventory under that same
account produced **309 AccessDenied command-line observations**. Executable paths were
readable for those observations (143 under System/Library, 112 under usr/libexec, 54 other),
but this is not a complete accelerator-vacancy proof. The current native guard explicitly
refuses inaccessible command-line inventory. No privilege, inventory guard, firewall or
sudo policy was changed. The precise first preparation exception was not exposed by the
generic authenticated 409, so these are demonstrated eligibility problems, not an invented
specific internal exception trace.

The prepare guard currently runs before any attempt journal is created. A rejected
preparation can therefore leave core's committed dispatch with no native attempt available
to return a stop receipt. The current scheduler retains those uncertain holds rather than
releasing them on HTTP failure; the real probe exposes this remaining reconciliation gap.
T125/T126 capture the required protocol/eligibility follow-up. No false stop receipt or
manual database release was introduced to make acceptance pass.

Restored API/scheduler **TRAINING_ENABLED=false**. Independently checked the running
scheduler flag and persisted probe: measurement **running**, command **dispatching**,
memory reservation **held, 223,986,114,472 bytes**. Stop/reconciliation lanes remain active.
This is unresolved counted ownership, not a passed memory profile or successful training.
Both node activations are complete; another activation request is not implied by these findings.

### Software corrections for the observed rejection/inventory gaps

Continued software work after the preceding diagnosis; the implementation gaps are now addressed
in source and a verified staged build. Their live protocol/acceptance closure remains pending.

- Added durable pristine-namespace measurement rejection: exact prepare metadata is privately
  fsynced before acknowledging a guard/hardware/expired-authority refusal. No local trainer or
  memory/disk hold is allocated. The rejected attempt cannot later prepare/start. Matching
  status/stop returns a scoped no-PID stopped proof; same stop command replays its persisted
  receipt. Unexpected attempt/config bytes, journal ownership, changed digest/fence/node/rank,
  corrupt/linked metadata or unavailable evidence refuse the proof. The normal active-attempt
  stop path remains unchanged. Rejection emits a named span, content-free structured log and
  bounded outcome metric. No new wire model or migration is needed.
- Scheduler's independent five-second stop lane rebinds **only** the original prepare after
  a stop conflict, then retries **the same** stop command. It never starts or supplies inputs
  on that recovery path. A prepare/stop HTTP refusal is not proof; only the subsequent typed
  receipt can release holds. Unreachable transport remains unknown and never triggers rebinding.
- Added kernel-backed platform-process classification for protected argv. Actual unprivileged
  `csops(CS_OPS_STATUS)` observations prove CS_VALID and CS_PLATFORM_BINARY, with readable
  noninterpreter executable metadata and stable birth identity. Python/script/proxy hosts,
  Coire/MLX/mflux paths, unsigned/invalid/reused/unobservable processes remain candidates and
  refuse admission. No path-only exemption, sudo/service-user change or privileged helper exists.
- Updated plan, node protocol and runbook with these bounded semantics. T125/T126 remain
  unchecked until live integration closes the retained dispatch and the measurement passes.

Measured verification:

- Red rejection/rebinding regressions were run before their implementations. The rejection
  test checks both guard refusal and expired authority, restart, immutable receipt replay,
  changed scope, forbidden later start and unexpected namespace ownership. Platform tests
  include Apple-signed Python/script/proxy hosts, missing signature bits and PID reuse.
- Focused native/API suites: **38 passed, no skips** before the extra expired-authority case.
- `COIRE_INTEGRATION=1 uv run --frozen pytest -q apps/coire-node/tests/unit
  apps/coire-node/tests/contract apps/coire-api/tests/unit/test_training_measurements.py
  apps/coire-api/tests/integration/test_training_measurement_transactions.py`:
  **730 passed, 1 existing platform skip**, 43.91 s.
- Enabled real-Postgres runtime/retention/measurement transaction plus measurement unit suites:
  **40 passed, no skips**, 20.55 s. An initial command referenced a nonexistent
  `test_training_measurement_protocol.py` and ran no tests; it was corrected to actual files.
- Full strict mypy: **796 files pass**. Ruff check/format (**1,510 files**), API OpenAPI
  freshness and Git whitespace checks pass. Shared wire shapes did not change.
- Built frozen wheels, verified 87 locked dependency wheels and staged immutable candidate
  **`/opt/coire/envs/0.2.0-f2e2149ce7ab`** on **both Studios**. Both pass 89-package compatibility
  and native smoke. Installed-wheel, real unprivileged process checks classify **309** protected
  processes on A and **306** on B as verified native platform processes, **0 unresolved**;
  measured entire inventory times **0.025957 s / 0.026936 s**. These are actual kernel/code
  identity observations, not fake process counts or full training acceptance.
- Built the matching scheduler image
  **`sha256:ffe4475bf14fc763db40709cc993ee6bcbd504f03b4a5a55ab95dedfe9a88461`**;
  repository image policy passes all seven rules plus core-harness separation. Trivy 0.74.0
  critical/secret gate passes; SPDX SBOM generated. Artifacts are external
  `016-operations/scheduler-rejection.{trivy,spdx}.json`.
- Deployed that immutable scheduler image through an external, narrowly scoped Compose
  override of the installed release. `up --wait --wait-timeout 60` reports **Healthy**.
  New admission remains disabled; the compatible recovery/stop lane remains running.

**Next operator prerequisite:** activate the consolidated `0.2.0-f2e2149ce7ab` candidate on
A and B using the existing staged service-preserving activation helper. Active nodes still
run `0.2.0-a306261daee6`; its successful activation/thermal checks above are not retracted.
The new candidate adds the two integration fixes discovered after that activation. Once
activated, verify the existing probe becomes inconclusive with scope-matched released holds,
then submit a fresh measured experiment and continue actual recipe/adapter acceptance.
No manual reservation release or passing training/profile result is claimed here.

### Final consolidated candidate and positive process-absence proof

Strengthened no-start proof with a complete current candidate-process scan for the attempt marker
and absence of its artifact namespace. This protects a worker surviving lost local state; a
pristine directory alone cannot prove it never ran. Missing/uncertain candidate inventory also
prevents recording or replaying a positive rejection receipt. Added negative controls for both
unknown inventory before rejection and unexpected process ownership after an earlier receipt.

- Final enabled native unit/contract, API measurement unit/transaction and Postgres retention
  selection: **736 passed, 1 existing platform skip**, 47.50 s. Full mypy **796 files**, Ruff
  check/format **1,510 files** and Git whitespace checks pass after the refinement.
- Rebuilt and staged the final immutable candidate on both Studios:
  **`/opt/coire/envs/0.2.0-2409eebc9b71`**. Both pass 89-package compatibility and native smoke.
  The intermediate `0.2.0-f2e2149ce7ab` is superseded and should not be activated.
- Ran the installed final wheel's expired-prepare/no-start/stop/restart/replay protocol on A
  using a **disposable private journal** and the real native candidate-process scanner.
  It passes with zero holds, no process spawn and unchanged production journals. This is
  installed-wheel protocol evidence, not live reconciliation of the core-held reservation.
- Exact noninteractive activation attempts for final candidate `0.2.0-2409eebc9b71` on A
  and B both refuse **`sudo: a password is required`** before executing the helper. The remaining
  root activation prerequisite is concrete; no service, permission or sudo policy was changed.

Use the existing staged activation helper with **final candidate `0.2.0-2409eebc9b71`** on
both nodes. The matching corrected scheduler remains healthy with new admission disabled.
Its recovery lane can then acquire the positive live no-start receipt, retire the inconclusive
measurement and release only its proved holds. Actual live release and fresh successful native
measurement/recipe-to-adapter acceptance remain uncompleted and are not inferred from these tests.

## Activations verified; live rejection reconciliation and measurement wiring (2026-10-06)

The operator activated `0.2.0-2409eebc9b71` on both Studios, retaining backups:
A `/opt/coire/state/node-activation-8l9uqzc6`, B `/opt/coire/state/node-activation-b2zl13cv`.
Independent authenticated health and training reconciliation return **200** on both,
with fresh **nominal** thermal readings at 15:41:08 UTC.

### Live T125 closure

The previously stuck measurement `3f6f0a87-18eb-4988-83a9-ac19fb71fc71` became **inconclusive**
through the running recovery lane. Its exact memory **and disk** reservations are **released**.
Authenticated A measurement status returns **200**, stopped with no PID and update 0, for the
matching job `01M48TY93WGBFW2G4WTC1AAKZT`, attempt `01M48TY93W782R7QSFB23VDBVV`, fence 1.
No database hold was manually released. T125 is checked for this specific protocol correction;
the broader training/reboot/release gates remain unchecked.

### Fresh probe and exact hardware mismatch

Temporarily enabled bounded acceptance and submitted fresh isolated-A measurement
**`a2c0cbd1-7562-48e2-a404-42c24cd2967d`** (**202**). It moved queued -> running -> inconclusive,
with no updates or profile approval. Native stopped status and both released reservations
confirm clean rejection rather than leaked ownership.

Core's requested hardware digest was
`43f6accb81d0b0c78a3a5665191056be4d8f810ed8dced1a9355c6a45a768954`;
the authenticated native capability digest was
`c956cdd7d889fad78c3f8edfda06d890c0c324e6e5cd46b108a5f458c58c5804`.
Both use A's 80 GPU cores and 274,877,906,944 bytes of RAM. The difference is exact:
registration/health advertise the installed package **0.2.0**, while the native helper used
shared Settings.SERVICE_VERSION default **0.1.0**. Fixed the helper to use the same package
version as registration/health. A red cross-component test compares against core's canonical
hardware digest with an intentionally unrelated service-version override; it now passes.

### Remaining measurement software completed in this slice

- Wired the missing internal `measurement_checkpoint` callback into the bare SFT worker.
  It sees evaluated full optimizer/adapter/RNG/sampler state, supports actual per-rank resource
  serialization, and emits no durable checkpoint/commit. It cannot mix normal checkpoint
  coordination or resume state. Ordinary training's fenced commit path remains intact.
  Source capability and pre-import negative-mode tests pass. Physical two-rank acceptance is
  still required; code capability metadata does not claim that result.
- Fixed terminal measurement command bookkeeping and persisted scope-matched stop receipts.
  A real-Postgres red regression reproduced commands remaining pending/dispatching after terminal
  outcomes. Completion now checks the persisted dispatch/owner, preserves the original idempotent
  submission receipt, records typed stop proofs and marks the command succeeded/failed in the
  same audited transaction. Missing proofs retain running uncertainty and counted holds.
  Historical terminal rows from the prior implementation are not rewritten.
- Corrected the offline worker fixture's explicit test-engine teardown. `Agent.close()` intentionally
  preserves engines; the fixture now stops its own exact engine and awaits confirmed stopped state
  before closing the agent, including failure cleanup. The first new gate reproduced one leftover:
  engine `4fa94cf7-859c-4b1e-b4ff-3d6c7d79df7e`, PID 36512, adapter under pytest run 12's
  `test_offline_native_cli_full_f0` artifact directory. Audited admin unload returned 202.
  After the corrected fixture gate, authenticated A inventory has **zero nonterminal engines**.

### Tests and built outputs

- Focused native source contracts/control/supervisor: **24 passed**, no skips.
- Enabled real-Postgres measurement transaction suite: **5 passed**, no skips, 5.80 s,
  including successful and inconclusive command/proof outcomes and missing-proof retention.
- Expanded native unit/contract + API measurement unit/transaction + Postgres retention selection:
  **739 passed, 1 existing platform skip**, 48.77 s.
- Built/staged immutable **`/opt/coire/envs/0.2.0-b7ef530d9303`** on **both Studios**;
  87 locked dependency wheels, 89-package compatibility and native smoke pass.
- The new real numerical callback test first failed on the previous installed wheel with
  `unexpected keyword argument measurement_checkpoint`. Corrected wheel gate on A's acquired
  offline Qwen2.5-Coder-0.5B 4-bit fixture: **4 passed, zero skips**, 17.83 s. This includes actual
  checkpoint serialization at completed updates 2/4 with optimizer moments, no measurement durable
  commit, normal checkpoint/pause, real extraction and adapter generation. After teardown correction,
  the full worker gate repeats **4 passed, zero skips**, 11.71 s, with confirmed test-engine stop.
  No numerical work ran on core and no model download occurred.
- Installed-wheel identity checks on A/B with an unrelated service-version override return
  **`43f6accb81d0b0c78a3a5665191056be4d8f810ed8dced1a9355c6a45a768954`** /
  **`26845a6af358b026c30c59ca3bfb53849f7ee0718bbc2c30a5c193e01ad18713`**,
  with the measurement hook available on both. These are identity/code checks, not profile approval.
- Full mypy **796 files**, Ruff check/format **1,510 files**, generated API OpenAPI freshness
  and Git whitespace checks pass.
- Matching scheduler image **`sha256:3779c6d18b99d1179d37d8c201f706b601ba369272ad3c4698aced795e9f2bce`**
  builds, passes all image policy rules, Trivy critical/secret gate and SPDX SBOM generation.
  Artifacts are external `016-operations/scheduler-measurement-final.{trivy,spdx}.json`.
  Deployed through the pinned narrow override; Compose `--wait` reports **Healthy**.

Both running API/scheduler are restored to **TRAINING_ENABLED=false**. Both measured trials are
inconclusive with released memory/disk; no successful profile or recipe-to-adapter result is claimed.
T127 tracks the hardware-identity live gate. The final candidate is staged but not active:
exact `sudo -n ... --candidate /opt/coire/envs/0.2.0-b7ef530d9303 --apply` attempts on both
Studios refuse **a password is required** before running the helper. Operator-authenticated
 activation of this tested candidate is the concrete next prerequisite for a fresh live measurement.

## Resumption: live identity closure and ordinary-attempt activation prerequisite (2026-10-06)

Continued under the user's request to finish feature 016. Branch remains
`feat/016-sft-training-jobs`; requirements checklist passes 16/16, no extension hooks exist,
and the session loop is disabled. Preserved all existing uncommitted work, including the
ordinary-attempt rejection implementation and its already-staged candidate.

### T126/T127 verified live

- Both active `/opt/coire/envs/current` links resolve to **`0.2.0-b7ef530d9303`**.
  Authenticated control health and training reconciliation return **200** on both,
  with **nominal** thermals and advertised installed version **0.2.0**.
- Read the existing successful measurement through the authenticated admin API:
  **`2af7e71e-dd73-4e92-b50b-1f2e9bc0850a`**, created at **17:14:13 UTC**, completes
  **12 updates**, peak physical footprint **1,398,834,784 bytes**, **zero swap growth**,
  thermal eligibility true, profile **`da5e34e7-7353-423e-a26d-1f7c0ede7d3b`**.
- Independently parsed the typed result and recomputed both report and resource-evidence
  digests. Report SHA-256 is
  `7ca07fb5f76de79fbb18ea66b87c4294cd47c15a70893be3c748926f145497ce`;
  hardware identity equals A's canonical
  `43f6accb81d0b0c78a3a5665191056be4d8f810ed8dced1a9355c6a45a768954`.
  Together with the recorded real kernel inventory checks, this closes T126/T127.
  It does not close recipe/adapter, physical recovery, distributed or mixed-workload gates.

### Fresh probe and retained ordinary-attempt uncertainty

- Temporarily enabled the existing narrowly scoped API/scheduler acceptance configuration;
  both services reported Healthy. Submitted a fresh isolated-A measurement through the
  authenticated admin path: **`271c04bb-1ad8-4670-93e3-c07d1de1d879`**, HTTP **202**.
  It remained **queued** throughout 180 seconds of bounded observation; no trainer started.
- Read-only persisted diagnosis identifies earlier job **`01M493BHBQ1BBTDZHDF3TEGD5P`**
  in **cancelling**, update 0. Its first attempt has a stored positive stop proof. Its second
  attempt **`01M493BRPDQB758P59E0VJYPCW`**, fence 2, has pending preparation and dispatching
  stop commands, no PID/stop proof, and a **pending 1,667,270,240-byte memory reservation**.
  A's measured residency is unavailable while ownership remains unresolved. No hold was
  released based on missing PID, HTTP conflict or elapsed time.
- The existing source already contains ordinary-attempt no-start rejection and exact expired
  prepare rebinding. The running scheduler has `stop_with_rejected_prepare`; the active
  `0.2.0-b7ef530d9303` node lacks `coire_node.training.rejections`. The newer immutable
  **`0.2.0-f8fa2f9ccede`** candidate exists on both Studios and includes that module.
  Both service-preserving activation dry runs pass. Exact `sudo -n ... --apply` attempts on
  both refuse **`sudo: a password is required`** before executing the helper.
- T128 captures this concrete activation/reconciliation gate. After activation, the running
  scheduler must obtain the authenticated no-start stop receipt and release only the proved
  attempt holds before new acceptance work. The source tests alone are not live closure.

### Independent data-fabric finding

Authenticated data-link reads return **A: ip_state=up**, **B: ip_state=down**;
both remain **rdma_state=degraded**. B resolves `coire-edge-a.fabric` to the original
declared address but its direct TCP connect to port 9400 returns **errno 65 (No route
to host)**. This is a current physical connectivity failure, not successful peer replication
or JACCL evidence. No route/interface/firewall mutation or suspended repair action was run.
Physical checkpoint/two-rank gates require actual bidirectional connectivity after diagnosis.

### Checks and preserved state

- `COIRE_INTEGRATION=1 uv run --frozen pytest -q
  apps/coire-node/tests/unit/test_training_prepare_rejection.py
  apps/coire-node/tests/unit/test_training_process_inventory.py
  apps/coire-node/tests/unit/test_training_measurement_supervisor.py
  apps/coire-api/tests/integration/test_training_runtime_postgres.py
  apps/coire-api/tests/unit/test_training_measurements.py`: **53 passed, zero skips**, 16.00 s.
  This includes real PostgreSQL expired-prepare rebinding and proof-only release boundaries.
- `uv run --frozen mypy apps/ packages/`: **798 source files pass**.
- Full Ruff check and format check pass: **1,512 files already formatted**.
  Generated API OpenAPI freshness passes. No schema changed in this session.
- Restored API/scheduler to **TRAINING_ENABLED=false** using the installed release plus its
  existing pinned scheduler override. Both Compose health checks pass; independent runtime
  setting reads confirm false in both. Existing cancellation/reconciliation lanes remain active.
  The queued experiment, successful prior profile, input provenance and uncertain holds are preserved.

Feature 016 remains incomplete at the concrete operator-authenticated activation prerequisite.
No commits, pushes, PRs, model acquisition or model/Metal workloads on core occurred.

## Operator activation verified; memory probe succeeds; physical mirror gate (2026-10-06)

The operator activated `0.2.0-f8fa2f9ccede` on both Studios, retaining backups
**A `/opt/coire/state/node-activation-n6xtc17i`** and
**B `/opt/coire/state/node-activation-kt6mouno`**. Independent control SSH checks confirm both
active links select that candidate. Authenticated health and training reconciliation return
**200** on both with nominal thermals. The previous activation blocker is resolved.

### T128 live closure and new measurement

- The scheduler's existing disabled-mode recovery lane moves earlier job
  `01M493BHBQ1BBTDZHDF3TEGD5P` to **cancelled**. Its second attempt
  `01M493BRPDQB758P59E0VJYPCW`, fence 2, now has a persisted scope-matched no-PID stopped
  receipt observed at **19:20:12 UTC**. Read-only checks prove **both** attempts' memory
  reservations are **released**, each with a positive stop proof. No database hold was edited.
- Re-enabled bounded API/scheduler acceptance and observed the existing queued measurement
  **`271c04bb-1ad8-4670-93e3-c07d1de1d879`** become **succeeded**, **12 updates**,
  peak memory **1,389,495,856 bytes**, profile **`f4db59e1-14d7-41d9-8361-67d05431357a`**.
  T128 is checked for activation/reconciliation, not inferred full training readiness.

### Real recipe trial and safe teardown

- The original output name belongs to the earlier job and remains reserved. A first retry
  reports 409 without creating a new job. Created a fresh external recipe with the same
  measured training configuration and a distinct output slug; authenticated validation
  returns **200**, resolved with no pending reasons.
- `uv run --frozen python scripts/validate-sft-training.py --api-url http://coire-core.lab:8180
  --recipe <external>/016-after-activation.yaml --report <external>/recipe-fresh-name.json
  --keychain-service coire-015-mixed-benchmark-key --timeout-s 180` submits job
  **`01M49AQ2639P8MJ7SKHKD8MNYF`** and verifies idempotent replay. It reaches **update 4**
  but exceeds the bounded observation deadline after three attempts, each awaiting its
  first mirrored checkpoint. `--timeout-s` is argparse's accepted abbreviation of
  `--timeout-seconds`; the command ran with a 180-second bound.
- Metadata-only report records `acceptance_deadline_exceeded` and `cancel_requested`.
  Independent read-only core checks verify the final state is **cancelled**, update 4;
  **all three attempts** have persisted positive stop proofs and **released memory**.
  Three update-4 checkpoints remain **replicating**, never committed or ready. No adapter
  output or successful recipe-to-adapter result is claimed. Uncertain disk/artifact ownership
  is retained for the normal retirement protocol.

### Refined data-fabric diagnosis (T129)

The prior port-9400 check was preliminary; modern data transfer uses **9401**. Checked the
actual data listeners and both directions explicitly:

| Observation | A | B |
| --- | --- | --- |
| Original bridge/address | active, original /24 | active, original /24 |
| Native en5 bridge member | present | present |
| Peer route/neighbor | bridge0, actual peer bridge MAC | bridge0, actual peer bridge MAC |
| Peer ICMP | 3/3 replies, mean 0.905 ms | 3/3 replies, mean 0.826 ms |
| Data listener | declared `.fabric`:9401, LISTEN | declared `.fabric`:9401, LISTEN |
| TCP to peer data port | success (errno 0) | errno 65 (`No route to host`) |

B's TCP to A port **22** also returns errno 65; B's control TCP to A `.lab` ports 22/9400
succeeds. Explicitly binding B's original data address before connecting to A:9401 still
returns errno 65. Another two ICMP samples succeed with zero loss. Both configured data
hosts/ports are correct. The generated link-local /30 aliases remain on en5; no address,
bridge, route, firewall or suspended repair operation was changed in this session.

On A, macOS Application Firewall reports **disabled**, block-all **disabled**. `/etc/pf.conf`
contains the default Apple anchor; `/etc/pf.anchors` contains only `com.apple`. Those files
do **not** establish the loaded runtime PF rules. Actual `pfctl -sr`, `pfctl -a '*' -sr`
and `pfctl -si` each refuse **`/dev/pf: Permission denied`**. `sudo -n pfctl -sr` refuses
**a password is required**. Do not attribute the TCP failure to PF without its actual rules
or packet evidence, or claim the bridge is broken from the TCP error despite working ICMP.
The next required input is operator-root **read-only** PF rules/status on both Studios;
any further corrective network/firewall change must follow the existing approval boundary.

### Preserved configuration and next gate

Restored API/scheduler **TRAINING_ENABLED=false** using the installed release and pinned
override. Both Compose health checks pass. Reconciliation remains enabled; no active training
memory holds remain. All external reports, checkpoint metadata and accepted intents are retained.
T129 captures the measured asymmetric TCP gate; T063/T109/T110 and feature completion remain open.
No commits, pushes, PRs, acquisitions or model/Metal work on core occurred.

## PF evidence received; runtime-specific connection failure isolated (2026-10-06)

The operator supplied root-readable PF rules and status from both Studios. Both report PF enabled
with the default Apple and Internet Sharing anchors. The printed Internet Sharing child contains
bridge100/loopback allowances and isolation-table drop rules. The wildcard anchor traversal itself
returns **DIOCGETRULES: Invalid argument** for `anchor "*"`; it does not enumerate every Apple
child or the isolation tables. These observations do not identify a rule matching the Studio data
addresses. No PF rule, table, policy, network interface, application permission or service setting
was changed.

### Client comparison supersedes the blanket TCP-failure description

Independent read-only probes on B establish:

- `/usr/bin/nc -vz -G 3 coire-edge-a.fabric 9401`: **connection succeeds**.
- Native `/usr/bin/ssh` to A `.fabric` using the existing authenticated key: **succeeds**.
- Native `/usr/bin/curl` to A `.fabric`:9401 with proxies disabled: **HTTP 404** from the actual
  data listener for an intentionally nonexistent route/artifact. No token or tensor is sent.
- `/opt/coire/envs/current/bin/python3` BSD socket to the same endpoint: **errno 65** in about
  **0.0014 s**. Python control-fabric TCP to A:9401 returns normal **connection refused** because
  that port correctly does not bind the control interface.
- Repeat with Python `-I -S`, literal resolved IPv4, protocol 0/explicit TCP, explicit source
  binding and `IP_BOUND_IF` binding to the observed bridge: **errno 65** in every case.
- Direct libc `connect` and public `connectx` in that installed Python process also return
  **errno 65**. This is not a Python library/proxy or asynchronous-client-only failure.
- A bounded **public Network.framework** probe in that **same installed Python process** reports
  satisfied path, unsatisfied reason 0, and an actual **ESTABLISHED** socket with source
  **192.168.100.12** and destination **192.168.100.11:9401**. A following ordinary BSD socket
  still returns errno 65. No alternative transfer mechanism was added to production.

The comparative result is a runtime/API-specific host eligibility problem, not a demonstrated
broken bridge or blanket B-to-A TCP drop. Authenticated node health still reports B's real BSD
data-link failure; native-client/Network.framework success cannot falsely clear that health gate.

### Runtime identity and privacy evidence

B's current Python resolves to the existing
`/opt/coire/python/cpython-3.13.15-macos-aarch64-none/bin/python3.13`, with ad-hoc code-signing
identifier **com.coire.node.python**, no bound Info.plist and no printed entitlements. The runtime
was not re-signed or replaced. Both nodes have **user-domain**, not system-domain,
`com.apple.network.local-network` preferences allowing their respective single peer address.
Their file modification times are **2026-10-03**, before the recorded Oct-4 reboots. No global
preference file is present. Do not imply a new pending reboot or change those preferences.

Consulted Apple's public **TN3179: Understanding local network privacy**:
`https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy.md`.
It documents automatic access for launchd daemons and SSH command-line tools, and requires root
plus restart for system-domain network exceptions. The observed Network.framework path does
**not** report `localNetworkDenied`. Consequently, do not assert a confirmed Local Network
privacy denial or prescribe a broad network exception from the BSD errno alone.

The first optional Network.framework state-callback diagnostic exited **133** due to the FFI
probe; it provides no state/permission evidence and was not used in production. Replaced the
external probe with the simpler public path query plus this process's own socket-state inspection;
that bounded probe completes successfully with the actual established connection described above.
No diagnostic imported an engine, tokenizer or model runtime.

### Next concrete input

T129 remains open. Compare the exact installed Python's BSD connection under operator-root on B
with the already-observed unprivileged failure. Agent SSH still cannot acquire root. This is a
read-only connection probe to the existing data listener; it does not grant a permission, change
a firewall or start training. Additional targeted PF/table or packet-header diagnostics may be
needed depending on the result; no cause or corrective configuration is guessed yet.

API/scheduler training admission remains disabled, reconciliation is available, and both Studios'
authenticated health/reconciliation remain 200. No further recipe was submitted against the known
blocked BSD transfer path. All existing partial checkpoints and job lineage remain private.

## Root comparison received; interpreter-binding defect reproduced and fixed (2026-10-06)

The operator's B root probe succeeds from the original data address to A:9401. Independent
unprivileged repetition still returns errno 65; authenticated B data-link remains down. The user
confirmed all existing Local Network rows enabled (two `python3.13`, two `coire-node-python`, and
`coire-node-probe`). Do not request the same permission toggles again or treat these enabled rows
as proof that the service actually uses the dedicated executable.

### Concrete identity mismatch

- Both active node environments resolve to shared
  `/opt/coire/python/cpython-3.13.15-macos-aarch64-none/bin/python3.13`, with signing identifier
  `com.coire.node.python`, CDHash **`8a6a08731c7538a4e2f712c021759c26e3edaf48`** and Mach-O UUID
  **`4C4C4480-5555-3144-A17C-C373D9442752`**.
- The already-present dedicated executable **`coire-node-python`** succeeds with ordinary BSD
  sockets as **mcteer on both Studios**. Its B CDHash is
  **`cad0db2c23cb888dea0e965f0d4337154540801e`**, UUID
  **`64169D99-7A54-5644-9C97-605E9D8EDFBD`**. This binary was not copied, re-signed, granted
  a permission or otherwise changed by these checks.
- B's actual service is `com.coire.node`, `UserName=mcteer`, parent PID 1, executing the current
  environment's `bin/python3`. It is the old executable binding that needs correction.

### Real uv reproduction and implementation

An explicit `uv venv --python <dedicated>` outside the configured managed root keeps the dedicated
binding and connects successfully. Repeat with the installer's actual
`UV_PYTHON_INSTALL_DIR=/opt/coire/python`, `UV_PYTHON_BIN_DIR=/opt/coire/bin`, `UV_NO_CACHE=1`:
uv **0.12.7** prints the dedicated interpreter path, but the resulting venv resolves to shared
**python3.13**. This reproduces the packaging defect without loading a model or changing networking.

- Added failing staging/reuse/publication and activation-identity regressions first:
  **4 failures, 16 passes** before implementation.
- `install.sh` now provisions under the same declared prefix, then unsets the two managed-location
  overrides **only for the explicit venv creation command**. A real repeated venv probe retains
  `coire-node-python` and connects as mcteer.
- Immutable environment digest is versioned **`coire-node-runtime-v2`** and incorporates selected
  executable bytes with the locked requirements and two workspace wheels. An older same-wheel
  environment with a different executable cannot be reused under the new identity.
- `stage_environment`/`publish_environment` verify the expected executable before smoke/move/link
  publication, including reused environments. The service-preserving activation helper rejects
  a legacy/shared or out-of-prefix runtime before changing the service or current link.
- The first full staging run correctly refused the wrong binding on both nodes, before native
  smoke; installer cleanup removed only its own unpublished staging directories. After fixing
  the managed-location shortcut, the full frozen installation succeeds on both.

### Verified staged output

- Built core/node wheels with `scripts/build-node-wheel.sh --local-only`; **87 locked dependency
  wheels** verified. Staged immutable **`/opt/coire/envs/0.2.0-aec82cd3280b`** on both Studios.
  Both pass **89-package compatibility**, exact bare-runtime hooks/imports/CLI smoke and final
  dedicated-executable identity verification. Existing active environments remain untouched.
- Candidate BSD peer connection returns **errno 0 in both directions as mcteer**. Candidate
  **`coire_core.net.DataFabricClient`** reaches the declared peer data listener on **9401** and
  receives **404** for an intentionally nonexistent artifact manifest. This is genuine ordinary
  socket/httpx transport success without privileged execution, proxy or control-fabric fallback;
  it does not claim artifact readiness or checkpoint-copy verification.
- Both updated activation dry runs pass and explicitly report the dedicated runtime path.
  Exact `sudo -n ... --candidate /opt/coire/envs/0.2.0-aec82cd3280b --apply` on each Studio
  refuses **a password is required** before executing the helper. Operator-root activation is
  the remaining concrete prerequisite, not another privacy-setting request.

### Checks and status

- `uv run --frozen pytest -q tests/unit/test_node_install.py tests/test_training_node_activation.py`:
  **20 passed, zero skips**. Cases prove wrong binding refuses before smoke/activation, correct
  symlink chains survive staging/publication, and activation refuses an out-of-prefix target.
- Full strict mypy: **798 files pass**; installer/helper/tests focused strict check: **4 files pass**.
- Full Ruff check and format pass: **1,512 files**. Installer `bash -n` passes. No wire schema or
  runtime dependency changed; source docs/plan record the packaging refinement.

T129 remains unchecked pending actual service activation, live authenticated data health and a
scoped mirrored-checkpoint trial. Training admission remains disabled and reconciliation available.
No service-user/privilege change, permission toggle, firewall/defaults/interface/route mutation,
shared-interpreter rewrite, commit, push or model/Metal workload on core occurred.


## 2026-10-06 continued acceptance and development credentials

T129 is now verified: both Studios run the dedicated installed Python runtime at
`/opt/coire/envs/0.2.0-aec82cd3280b`. The former activation prerequisite is resolved.

A real distributed crash was traced to both ranks publishing naturally different timing
and memory samples into one immutable metric identity. Both authenticated rank mailboxes
are now retained, while rank zero supplies canonical loss metrics. Two regression cases
(first arrival from either rank) failed before the fix and pass afterward.

Live single-rank QLoRA job `01M49WT7PV2BCSY0TRDS6SHEYN` and fixed two-rank QLoRA job
`01M49XM6ZRRS7ARYYJPK9NQ33E` each completed 12 updates, verified physical checkpoints
at updates 4/8/12 on both Studios, and passed exact-adapter gateway inference. The latter
published adapter `b2a0186c-3ac0-50f0-8ccf-ffe90145b585`. Earlier failed distributed
attempts were stopped with reservation-release proof; no failed checkpoint was published.
Acceptance report metadata is persisted outside Git beneath
`~/.coire/projects/coire/releases/016-finish-20261006/evidence/`.

Verification: 2,574 local tests passed (two platform/opt-in skips independently accounted
for), 156 PostgreSQL integration tests passed without skips, 19 actual tiny-model engine
tests passed on Studio A without skips, strict mypy passed across 799 files, Ruff and
OpenAPI/generated TypeScript freshness passed. Web: 171 tests passed, lint and build passed.
Node installer/activation regression suite: 20 passed. The database lease test now bounds
its database timestamp with the database clock, avoiding a host/VM clock race.

The administrator training runbook is now rendered at `/docs/runbooks/sft-training` with
alert-compatible anchors and permission checks. Build-only `@types/node` 22.18.8 (MIT)
supports embedding the checked-in runbook; both web lockfiles are updated.

The user explicitly authorized configuring development testing credentials. A dedicated
audited API key `f0c6d256-c6fd-4877-a5df-eedf869c3d9a` with admin/chat/mcp scopes is
stored exclusively in login Keychain service `coire-016-development-key`; no secret is
written here or in Git. Existing node authentication remains intact.

Both Studios have the tested digest-pinned harness and relay images loaded and a paired
activation helper staged at `/Users/mcteer/coire-stage/016-harness-images/activate.sh`.
Their root-owned system service environments still have empty image settings. Ordinary
SSH works, but noninteractive sudo requires a password and root SSH is unavailable.
This is OS deployment access, distinct from the newly configured Coire API credential.
Genuine harness acceptance, the full fault matrix, and measured local pinned-ops
coexistence remain open; successful training happy paths do not close those gates.

Prometheus training rule tests passed using the existing pinned Prometheus image,
read-only rules, network disabled, and throwaway `/tmp`; this verifies baseline missing
instrumentation, idle behavior, stall/recovery/checkpoint/protection firing and clearing.
T115 and T119 are checked against the recorded test evidence.

Final image policy, critical vulnerability/secret scans and SPDX SBOM generation passed
for API, scheduler and web. Exact deployed image IDs:
- API: `sha256:563be75fa07f8b75d3501216f5c47f15ba067ac815bad1721496f77de9de7f7c`
- scheduler: `sha256:42471361e419bd327660671264810d467453c198751e490762e986f1e9ac040f`
- web: `sha256:0a8ef863a026425e333d560d9d7981557d3f834665125b311ca136a2bb25f733`

Deployment uses the persistent private release `compose.json`; `rollback.json` preserves
prior control-plane image/environment settings. Existing ops/file-worker containers were
retained. API/scheduler/web report healthy. Dedicated-key `/api/v1/me` and admin node
health reads returned 200; both Studios are healthy. Rendered runbook route returned 200.
Final scan/SBOM/test logs are persisted in the private evidence directory.

No root credential was found in available authenticated SSH or Keychain services; root
SSH is rejected and `sudo -n -l` requires a password. Creating an API key did not change
OS privileges. No firewall, network, CORS, sudo or authentication policy was weakened.
The durable [handoff](handoff.md) documents remaining work without marking it complete.


## 2026-10-06 resumed completion after operator activation

Operator activation succeeded on both Studios, with backups
`/opt/coire/state/node-activation-30q6qype` (A) and
`/opt/coire/state/node-activation-dy9nxv2r` (B). Authenticated node health now
reports `run_images_configured=true` on both. Matching agent/relay pins are
configured in the persistent API/scheduler deployment; T131 is complete.

Live pause/resume exposed another real bug: comparison of raw serialized
Principal scopes depended on unordered set serialization across processes.
The private smoke resolver now compares validated Principal identities, retaining
current authority and immutable-lineage checks. The new real-Postgres regression
failed before the fix; all 13 private smoke tests pass afterward.
API image: `sha256:c94c895d7b76ec28dddf8f6425d434c5e283d8aa650b635e8c927ace1c410fc8`;
scheduler: `sha256:76c40c5d0bc9b3901ac3dd465b1a6bb0dbc3ba188ec945f45720f5e1e4418bf7`.
Both passed image policy, critical vulnerability/secret scan and SPDX generation.
The original affected trial `01M49Z0960YPGVCS4K2H57XC4K` was cancelled through
the audited control endpoint; its report remains a failure, not overwritten.

Corrected pause trial `01M49Z8A5PVV8JP5W4668CFHR0` passed: durable pause in
4.213 s at update 4, resumed full state, reached update 12, mirrored 4/8/12
checkpoints and passed gateway inference. Separate cancellation trial
`01M49ZAA0QGMAFYGT20RKQWVQW` passed in 1.094 s at update 4 without publication.
Reports: `pause-live-fixed.json`, `cancel-live.json` in the private evidence directory.

Genuine Studio-owned exact-adapter MCP plan execution succeeded with target
adapter `d7615f77-aef8-5b37-8d4a-1f61c2d4b124`; authenticated MCP advertises
exactly research/plan/apply. No harness ran on core. Independent CLI evaluation
`36c56122-7266-4bfd-9a62-6a536d0ec1af` was an infrastructure-error trial during
cold placement; its retry passed tool/structured-output/long-context but failed
edit application. This actual quality failure leaves the tiny adapter unverified
for write-capable runs; the gate was not loosened.

Live datasets text `329fbaf8-5f23-5ee6-942c-f87de255c055`, prompt/completion
`73ea9c36-0a57-59ea-a7d7-6ecffb7ec2ec`, and tool conversation
`bb00318b-b8bc-5756-8916-7ee1b12d82a9` each reached ready with 17 rows, including
a duplicate, with idempotent upload replay and native tokenizer analyses.
Malformed JSON, image rows, unmatched tools and empty targets produce failed
dataset records and content-free row diagnostics. Duplicate YAML keys, aliases
and executable tags refuse with 422. Metadata is persisted privately.

The disposable migration suite passed all 9 tests, including real revision
upgrade, protected downgrade refusal and clean downgrade/upgrade round trip.


## 2026-10-07 UTC — recovered acceptance evidence and live continuation

Both authenticated rank-failure trials passed: rank 0 job
`01M49ZR4ABGBEW268H5YFBAMCS` and rank 1 job `01M49ZVMYPCZZYEA8WH9SFBBF2`.
Each injected failure used the node-owned, scope-matched stop protocol after a
committed update-4 checkpoint. Peer death proof preceded reservation release;
generation 2 resumed full state, completed update 12, mirrored checkpoints, and
passed gateway inference. This proves rank loss; physical data-link and corrupt
transfer acceptance remain separate open gates. Observer failures and cancelled
trials are retained independently in private evidence.

Typed form submission also passed the acceptance driver, with the same normalized
intent as YAML. LoRA job `01M4A0513QY6JZ74AYK8V0B9RN` and DoRA job
`01M4A070MB25DHGAPKR1GGWDD0` each paused at update 4, resumed to 12, physically
mirrored all 4/8/12 checkpoints, and passed exact-adapter gateway inference.
Pause latencies were 3.231 s and 3.182 s respectively. Their adapters remain
independently unverified for writes, as required. Together with the recorded QLoRA
trial, all three parameterizations have real train/checkpoint/resume/serve evidence.

Three existing real Studio-A reboot trials were recovered from durable jobs and
correlated with native `last reboot`: jobs `01M49FA0DMCBPY25VHC4J02M5D`,
`01M49G62VS5QY76S883KDBPJ1S`, and `01M49GRNG7CDE7S8BG2ZJTSC94` paused after
update 4, then physically rebooted at 20:44, 21:00, and 21:10 UTC on October 6.
Each resumed to update 36. Against uninterrupted job `01M49FWSHGN1CHD81S3QXWYG34`,
all 32 subsequent train losses have maximum absolute difference 0.0;
all four adapter tensors and ten optimizer tensors have maximum difference 0.0.
Comparisons used rtol/atol 1e-4/1e-5 for losses and 1e-5/1e-6 for tensors.
Final RNG, sampler, optimizer-tree, runtime and update state match exactly;
normalized config identity also matches. NumPy read-only tensor comparison ran
on Studio A. These were paused reboot trials, not running-process power cuts.
Private evidence: `reboot-{identity,state,loss,numeric}-comparison.json` and
`reboot-checkpoint-inventory.jsonl`.

The sustained Studio-B memory pilot is currently running as measurement
`68919434-69c1-4f61-bde2-f06faf90c3f0`, for 16,384 actual updates. An oversized
65,536-update pilot was stopped through authenticated node ownership and retains
its stop proof; it is not a passing profile. Coexistence still requires the genuine
local pinned ops fixture and complete 15-minute baseline/mixed phases.
Diagnostics collector, Tempo, Loki and Grafana were enabled internally with the
existing hardened service settings; actual node/control spans are present in Tempo.
No external diagnostic ports were exposed.


The already acquired/verified 1.5B base passed a fresh native memory profile
`302cdf89-b673-4aaf-9f2c-de9f61883de2` (12 updates; peak 1,947,453,504 bytes).
QLoRA job `01M4A0J6FK20D54DHFTR7EPNRP` paused in 2.150 s at update 4,
resumed to 12 and served adapter `1ace2fef-b464-545a-96f5-b79897ec7a28`.
Independent harness evaluation `20e23e2d-68b3-45c0-b193-dfdf02e33439` genuinely
passed all four scores at 1.0. Subsequent Studio-owned MCP apply run
`4fd45e9e-b048-4edf-9a47-084b38c6e595` succeeded against that exact target in a
disposable workspace, without push. This is a positive write-verification trial;
the failed 0.5B evaluation remains failed. Chat, SSE and Anthropic Messages passed.
Legacy `/v1/completions` returned 404 and remains an uncovered quickstart gate.

Live checkpoint promotion exposed an unwired temporary API extraction flag.
The installed scheduler/node extraction implementation now receives promotion
intents without an unset app-state flag; disabled admission still refuses.
Seven route contracts passed, with strict typing/format/lint passing. Before-fix
promotion returned 503; after deploying scanned/policy-checked API image
`sha256:7600449fe85043373be1c6650a7a36f00848f383725e583b88ef0a762c681fc8`,
update-4 promotion produced ready, independently unverified adapter
`07a1264f-ec4c-5756-b84e-f35ae035d507`. Metadata remains private.

Final opt-in integration rerun: 157 passed, no skips, 181.73 s, with
`COIRE_INTEGRATION=1 uv run --frozen pytest -q apps/coire-api/tests/integration
 tests/integration/test_training_targets.py tests/integration/test_training_distributed.py
 tests/integration/test_exact_adapter_resolution.py`. The initial invocation without
opt-in ran 56 and skipped 101; it is not the acceptance result.

The 16,384-update B memory pilot passed with profile
`63365c51-f865-497e-a04b-b5f18af15e14`. Actual throughput would finish that recipe
before the required complete mixed phase, so a separate 32,768-update memory
measurement `ccce847e-ece2-4943-8942-6ecbd660b702` is running. Existing reports
remain immutable evidence; no run's declared update count was edited in place.


The legacy completion gap is closed: typed coire-core request/response contracts,
canonical single-user-turn execution, translated JSON/SSE choices, preserved usage
ASGI disconnect finalizer, and explicit unsupported-option refusal. Twenty-nine
legacy/existing gateway contracts passed; live exact-adapter JSON and SSE returned
200/text_completion with the advertised selector and terminal DONE.
API `sha256:cab8be8d92bf16c97545454150d948047f252234ad6e7734c46243a2102d2540`
and scheduler `sha256:27bc6315fdba696240c7e6f7a3071ad8b369bb2c4277832677eb8693479b6e15`
passed policy, critical/secret scans and SPDX generation. API is deployed; scheduler
replacement waits for the in-flight measurement to finish. OpenAPI/TS regenerated.
Local suite: 2,583 passed, 2 platform/opt-in skips, 403 deselected, 95.05 s;
web: 171 passed; CI strict mypy: 801 source files pass; Ruff check and format pass
(1,516 files). An extra mypy invocation including root legacy tests found 68 errors
in 19 preexisting files outside the CI target; no new module errors. It is not
reported as a passing gate.

Owned node-agent crash trial `01M4A1M7PKQ767HVJTJ6B970Z7` completed successfully.
The SSH user owns the exact launchd-labeled agent; its PID/create-time and UID were
validated before SIGKILL. Launchd restored service in 2.119 s. Authenticated status
immediately re-adopted trainer PID 6382 with unchanged create time. Controller
subsequently fenced/stopped generation 1 and recovered generation 2 from the complete
update-4 checkpoint, then completed 12 and exact-adapter inference. Thus recovery
passed without duplication, while continuous survival of the original trainer was
not demonstrated. The initial observer interrupted by API deployment before injection
was cancelled, and its failed report was retained separately.


### Native worker telemetry correction and current packaging (2026-10-07 UTC)

The full 32,768-update Studio-B isolated pilot `ccce847e-ece2-4943-8942-6ecbd660b702`
succeeded with peak 1,336,788,600 bytes and zero swap growth; profile
`8c56e9f3-b0e5-4c5c-b074-c0b229d3e37b`. This precedes the telemetry correction and
is retained as its original runtime evidence, rather than relabeled as a new profile.
Actual Tempo search found parent training-start/event-ingest/mixture spans but no
native execute span: worker entry points lacked SDK startup and their minimal
child environment omitted the configured exporter endpoint. Both entry points now
initialize the existing SDK and forward only configured OTLP_ENDPOINT; load and
execution spans have content-free job/attempt/node correlation. No dependency added.
The SDK-export and credential-isolation regressions failed before correction and
passed afterward. Full node unit/contract suite: 719 passed, one preexisting
non-Darwin physical-footprint fallback skip; strict CI mypy: 803 files passed.
The frozen 87-dependency/89-package wheel graph was staged on both Studios without
changing their active links, into `/opt/coire/envs/0.2.0-fac9a3576ce2`.
Against the installed candidate on Studio A, all 19 enabled native engine tests plus
the isolated real-SDK export regression passed: 20 passed, no skips, 35.70 seconds.
Activation and fresh native profile/diagnostic acceptance follow; T134 remains open.

Rebuilt MCP, migration, web and ops distributions passed bare-image policy, critical
vulnerability/secret scanning and SPDX generation. Initial simultaneous Trivy scans
for MCP/migration/web failed on the shared cache lock; sequential scans passed.
Immutable image IDs are preserved in private release manifests and scan reports.
The unavailable old ops image was identified before deployment/admission; no
coexistence trial was admitted by the failed external observers.

Gateway overhead used 100 actual 4,000-input-token Studio streams plus one other
request; the existing versioned Prometheus p95 query measured approximately
14.9927 ms at the retained sampling times, below the 20 ms limit. Workload and
query receipts are private metadata-only evidence. Remaining regression/diagnostic
acceptance is still tracked by T120/T122.


Both user-owned immutable runtime links were switched through the existing verified
installer, followed by exact UID/PID/create-time-validated launchd-agent reloads.
Authenticated health passed on both; installed root plists, Keychain settings and
harness/relay image pins were preserved. Private activation receipts retain the
previous environments for rollback. Fresh Studio-A measurement
`ab386546-f0cc-4a7c-84bd-ac483ff22da0` succeeded after 12 real updates, profile
`ae2c694b-5720-4b9b-8d64-8ba4d34bdcda`. Tempo trace
`4293bb6b415bbdb220dd91eeba7f5f14` contains 49 spans, including actual bare load,
render/preflight, completed-update execution and measurement serialization.
Prometheus now receives native completed-update counters from both nodes.
Fresh full training `01M4A3ATGKEJAYDF0A0SRDB9RM` completed 12 updates, all mirrored
checkpoints and exact-adapter gateway inference, output
`bacc55af-92bd-5934-a78d-8430d0153b72`. The initial reused output name was correctly
refused; the distinct-name retry passed, retaining the failed observer receipt.
Studio-B current-runtime long pilot `09fe4487-018f-40a7-b27f-540733e9c799` is running;
its persisted external sequencer waits for real completion before warming the
pinned Studio ops/base and verified-adapter chat coexistence fixtures.

The running Prometheus image was found to predate the training rule COPY: its
actual rule API exposes no coire-training group. The checked-in Dockerfile already
packages the rules, so a frozen rebuild and image gates are underway before live
fault alert acceptance. This deployment gap is not counted as a passed alert test.


The independent recipe/form and dataset/split/mixture acceptance gates now have
recorded live results: form job `01M4A018SY5Y0790Q24GVR7AWW` completed 12 updates
and equal normalized YAML/form intent; all three parameterizations pause/resume,
mirror and serve. Already acquired 1.5B output additionally passed genuine exact
adapter verification and Studio write-harness execution. T063 is closed on this
evidence, without claiming remaining two-rank/mixed faults. T092 is closed on
three-format uploads, deterministic duplicate-group splits, content-free row/YAML
refusals, repeated actual persisted-manifest mixture compilation and the enabled
native mixture/formatting tests. The private rejection receipt now distinguishes
202 upload acceptance from 200 diagnostic reads and final FAILED state.

OpenAPI freshness passed again, and independently regenerated TypeScript is
byte-identical. Existing broad gateway/chat/image/run/registry/failover tests,
web tests and the measured approximately 14.99 ms gateway p95 close T120.

An operational private consistent Postgres backup restored into a disposable
separate database with migration `0031_sft_training`, 32 training jobs, 102
checkpoints, 25 adapters and 11 dataset revisions; the selected terminal 12-update
job was intact. Disposable database was removed. Core dataset-volume backup
restored 12 files / 30,245 bytes with every file SHA-256 identical, then the
isolated restored files were removed. No tensors were copied onto core. These
prove control-plane/dataset restore, not independent off-host tensor backup.
Queued authority revocation used a separate audited Keychain-held test key.
Job `01M4A3JQK7R695FNNDC2AADPTW` moved from queued to cancelled/unauthorized at
update zero with no attempt or adapter; test key was revoked through admin API.
The active Prometheus rebuild is now healthy and exposes all six coire-training
rules. Real stalled-trainer alert firing/clearing remains under observation.


Owned stall trial `01M4A3FEY6R96S2C8W7G9A108A` suspended only the validated native
trainer PID/create-time/UID at update 4. Prometheus transitioned the genuine
CoireTrainingProgressStalled alert to pending and firing (observed at 396.12 seconds,
including the exact actionable runbook annotation), then cleared after recovery.
The observer and independent 420-second native timer both bound resume to the same
owned process identity. Controller recovered a fresh generation and finished 12
updates, all mirrored checkpoints and adapter inference; continuous original-worker
survival is not claimed. Running authority revocation trial
`01M4A3YANB1Z3GYNEGCNA7S803` was cancelled/unauthorized at update 4 in 0.855 seconds
with no adapter. Its separate audited Keychain test key was revoked.

The real rule API also exposed persistent replication-overdue alarms from older
cancelled attempts: their intentionally retained incomplete checkpoint rows were
still treated as active transfers despite complete rank death proof. T135 records
a read-only reducer correction: exclude abandoned terminal/superseded lineage only
with all-rank immutable stop proof. Current transfers and uncertain old ranks stay
alarmed. Real PostgreSQL cases failed before (cancelled and fenced), then all 14
baseline/metric/export cases passed after; strict mypy and Ruff passed. No artifacts
are deleted, reservations freed or partial checkpoints marked ready by this change.
Rebuilt API/scheduler deployment waits until the long native measurement completes.

The bounded link-loss helper is staged and its read-only check passed on Studio A.
It waits up to three hours for a fresh private trigger naming an actually running
two-rank job/fence/attempt and its committed checkpoint, independently verifies
native ownership, lowers only the tested rdma_en5 data interface for 30 seconds and
restores it with an independent 45-second watchdog. It installs no sudo policy or
persistent service. Operator root activation is the remaining prerequisite for that
specific fault, while other acceptance continues.


Current-runtime Studio-A LoRA, DoRA and verified 1.5B profiles each passed 12 real
updates: LoRA profile `5b949a3f-984b-4c5f-aef8-50674572b52a`; DoRA measurement
`89e8f670-1ec3-4581-a333-08336118c04c`, profile
`5dcbfafa-2e9b-45c2-9ab4-803975a80306`, peak 2,263,402,776 bytes;
1.5B measurement `637c7514-771c-4e25-aa03-194af423f27a`, profile
`d72c4447-bb37-4eaf-8e64-16fa4927f570`, peak 1,954,171,064 bytes. Two external
observer mistakes (wrong report field and a dot in an output slug) were corrected;
no successful profile was repeated or relabeled and no API validator was loosened.

Ordinary-user live credentials got 403 for training jobs/recipes/adapters and saw
no private selectors. The temporary user/key were disabled. A separate publication
trial on disposable adapter `bacc55af-92bd-5934-a78d-8430d0153b72` proved private
hidden -> published visible -> unpublished hidden/404 -> retired 404 even to admin,
and exact retirement replay. Verification stayed false; no write gate changed.
Temporary publication user/key were disabled and the disposable adapter is retired.
Three separate Studio-A instances were then warm simultaneously: 1.5B base,
verified final adapter, and private promoted checkpoint adapter. Actual successful
requests were joined to durable usage and native-engine rows, proving distinct
instance/engine IDs and the exact expected variant/adapter IDs for each request.
All three owned fixtures were drained afterward.

The current-runtime 32,768-update B pilot was making approximately 11.47 updates/s:
its predicted training duration plus the required 900-second baseline would exceed
the existing one-hour probe bound. It was stopped through the authenticated typed
node lane with positive death proof and retained as inconclusive, without a valid
profile. A separate frozen 16,384-update request was admitted as memory pilot
`e4a6e233-5e20-4284-86ff-c33121e72621`; the sequencer will require its complete
real result before reusing that same configuration for 900-second baseline/mixed
phases. The earlier pre-SDK 16,384-update result was too short and is not substituted.
No deadline, sample requirement, swap or first-token gate was relaxed.


Native CPU dataset analysis had the same child exporter startup omission. Its
valid-owned-envelope regression failed before correction, then the 23 analysis/
SDK/ownership cases passed. Analysis forwards only configured OTLP_ENDPOINT and
initializes the existing SDK after scope/deadline validation; RSS/deadline guard
and credential isolation remain intact. Full node suite: 720 passed, one existing
non-Darwin fallback skip; CI strict mypy: 803 files passed. Candidate
`/opt/coire/envs/0.2.0-efa1dbea205e` passed all 20 enabled native/SDK checks on A,
no skips, 31.86 seconds. It differs from fac9a3576ce2 only in the two analysis
modules; every numerical worker/measurement/telemetry/core source module is byte
identical (private digest comparison retained). Only quiescent A was activated;
B stays on its measured immutable runtime until its acceptance finishes.
Fresh synthetic dataset `7ed0acce-772d-5e01-a97b-6031a989d71c` / analysis
`dbd6ab54-5d63-48be-ad65-3b1c19f603ed` reached ready with eight rows. Native
Tempo trace `45efc2cb9a45d0ebdb33c59bd2becfb9` names the actual guarded CPU
analysis stage, duration eight milliseconds. No model/tokenizer ran on core.

Diagnostics-off verification now uses the actual lean collector, API
DIAGNOSTICS_ENABLED=false and stopped Tempo/Loki/Grafana containers, preserving
Prometheus/Alertmanager and durable audit/history. The bounded exact owned-agent
control outage/cancellation trial is under observation; counted holds persist
while control is unreachable and the real protection-overdue alert has fired.
Final cancellation/proof/history/audit and clearing checks remain outstanding.


Diagnostics-off control-outage trial `01M4A54YH77JTCSQK39ZY9PK2R` passed. Only the
UID/PID/create-time-validated launchd node agent was suspended for a bounded 75
seconds, with an independent exact-identity resume timer. Cancel was accepted
while control was unreachable. At 0/10/21/31/41/52/62 seconds the job remained
cancelling and its reservation stayed held with no death proof; no timeout-only
release occurred. Actual CoireTrainingProtectionOverdue fired and reached local
Alertmanager with its runbook annotation. After agent restoration, cancellation
was confirmed at the 82.72-second observation with native death proof, released
hold and no adapter. Healthy-node five-second cancellation is a separate earlier
passing trial; this outage does not falsely claim that bound. Ten durable events,
five loss metrics, checkpoints and cancellation audit remained readable while all
three diagnostics backends were stopped. The observer's events/items metadata-key
mistake occurred after terminal proof and was corrected without repeating the
fault or changing service behavior. Actual protection/lease-expiry gauges returned
to zero and the protection alert cleared. Diagnostics backends/collector were
restored; baseline/history data remained. This proves outage protection and alert
delivery, while a genuine mixed-chat latency guard and remaining thermal/memory
matrix still belong to T112/T122.

### Continuing acceptance: diagnostics outage, artifact restore and resident health

With diagnostics disabled, lean collector configuration and Tempo/Loki/Grafana stopped, job `01M4A54YH77JTCSQK39ZY9PK2R` accepted cancellation during a bounded 75-second node-agent control outage. Reservations stayed HELD until native death proof; cancellation finished after 82.717 seconds without publishing an adapter. Protection-overdue fired and cleared. Durable events, metrics, checkpoints and cancellation audit remained available. This does not claim the healthy-node cancellation bound during an outage. Private receipts: `partition-off-live.json`, `partition-off-clearing-live.json`; diagnostics backends restored afterward.

Studio A independently backed up and restored checkpoint updates 4/8/12 and the verified final adapter for job `01M4A0J6FK20D54DHFTR7EPNRP`. Typed manifests and every file SHA matched, including adapter, optimizer and sampler/RNG state files. Backup stays private on the Studio; no tensors were copied to core. Isolated restore removed after verification. Receipt: `studio-artifact-backup-restore.json`.

Current-runtime isolated memory pilot `e4a6e233-5e20-4284-86ff-c33121e72621` completed all 16,384 updates. Genuine ops answered through the pinned Studio B base instance. Coexistence `3ce98392-255b-41a1-912d-4a096158b5e0` remains queued: its admission incorrectly required the sharded-only rank-health flag on single-node residents. Read-only rollback inspection proved frozen input identity unchanged and both owned engines READY while their rank flags were false. A regression failed before replacing that check with exact owned-engine health/identity checks. No node observation or database health flag was altered to bypass admission. T136 tracks tested deployment and remaining genuine workload.

The checkpoint-alert fix is deployed. Actual Prometheus `coire_training_checkpoint_pending_oldest_seconds` is 0, snapshot timestamps remain fresh, and `CoireTrainingCheckpointReplicationOverdue` has no active series. SQL regressions retained current/unproven transfers. Private receipt: `checkpoint-alert-clearing-live.json`; T135 complete.

Admission correction then exposed a distinct native identity error: the guard compared `EngineStatus.engine_id` with logical resident `instance_id`. The schema now carries an authenticated frozen `resident_engine_ids` map; core binds verified member engines, node compares their exact targets/readiness, and measured gateway requests recheck the binding. Logical instance IDs remain the keys for request-lease snapshots. Contract tests use different instance and engine IDs and reject substitutions. The failed experiment `3ce98392-255b-41a1-912d-4a096158b5e0` was fenced inconclusive with no profile; it is not positive coexistence evidence. Fourteen focused Postgres/node contract checks pass, including stopped/wrong-instance/wrong-port engines; source runtime packaging/deployment continues.

Both Studios activated immutable node environment `0.2.0-89913525cb7e` with authenticated health and preserved harness pins. Studio B re-adopted the two resident engines (same exact IDs); no active training/measurement was present during activation. API `sha256:ee648cc3eee526dce6aa442217181ea70f83a7f633920dc0ca9a97ffe5f586b8` and scheduler `sha256:b9c67dd4d64f69df35e24286fc7a1934065ee93d61b3b3c8d8ea43bfe0e799d0` pass all image policy rules, Critical/secret scans and SPDX generation. Full node suite: 720 passed, one existing non-Darwin fallback skip; enabled native gate: 20 passed, no skips. Training integration selection: 123 passed, 14 deselected; focused gateway/node checks separately 14 passed. Strict mypy checks 803 source files; Ruff/OpenAPI freshness pass.

Pre-first-checkpoint trial `01M4A6FSASWNDAD6MNMJ35YMC7` stopped its exact owned rank at update 0 with no checkpoint (0.027-second positive stop receipt); a fresh fenced attempt started at zero and finished update 12, producing mirrored checkpoints 4/8/12 and a successfully served private adapter. Receipt: `step-zero-recovery-live.json`. Actual native MLX recovery in an isolated Studio copy of backed-up full state chose update 12 initially, then fell back to update 8 after a checksum-invalid newest optimizer tensor file. Corrupting every remaining valid state file produced explicit refusal. Production artifacts were untouched. Receipt: `native-corrupt-checkpoint-recovery.json`.

Fresh bound coexistence `4c75265a-8736-4fbc-8722-7e9e8da1e182` admitted and produced 30 actual 4,000-token completions per target. Its rolling baseline p95 breached 1.5 seconds, so the normal protective gate ended it inconclusive before training, with no profile. Native/core holds were stopped and released. One initial request measured 1.720 seconds and a later outlier 2.440 seconds; most samples were 0.23–0.62 seconds. No sample was dropped and no latency threshold changed. Ordinary authenticated sequential and simultaneous 4,000-token diagnostic requests to the same exact targets measured first tokens at 0.145–0.518 seconds; these diagnostics are not a qualifying 15-minute profile. A fresh unchanged frozen workload is being retried with already-warm residents and no unrelated A training work. Private failed request/result receipts remain distinct.

Native checkpoint corruption/missing-state checks above used real saved MLX tensor files from the verified 1.5B run, not NumPy mock tensors. The isolated native restore verified full state before allocation and loaded four trainable tensors and optimizer state at update 8. The pre-first-checkpoint trial produced a distinct fence-2 attempt; its initial recovery step was explicitly zero. Scheduler-only restart remains intentionally postponed until the coexistence experiment is terminal, because restarting the measurement owner must fence an interrupted experiment inconclusive.

Current worker trace query for the successful step-zero run confirms actual execution export, but revealed ordinary load/render spans were separate unowned roots. A job/attempt/node-bound `coire.node.training.worker` parent now encloses ordinary native execution. A fresh-process SDK regression verifies load/execute children share that exact parent and trace; two telemetry tests pass. No occupied node has been changed during the frozen experiment. Measurement workers already wrap all phases in an owned parent. This follow-through remains under T134 until immutable packaging and live attribution are verified.

Warm eight-second-arrival experiment `bbf9eda0-27f8-4af3-8537-6fd8f7aba1b7` also stopped inconclusive on rolling baseline latency, without a profile. Connection-level diagnostics exclude DNS/TCP setup as the long delay: both targets sometimes wait roughly three seconds for control HTTP response headers, while TCP setup remains tens of milliseconds. Forty authenticated two-target diagnostic requests yielded six >1.5-second samples; a subsequent forty yielded none (p95 0.622 seconds, max 0.723), demonstrating variable latency rather than a qualifying profile. Raising only the diagnostic client idle expiry did not preserve connections because the node server still closes idle connections; no unproven transport change was shipped. All these diagnostics are private metadata and not acceptance gates.

A new explicitly frozen two-second-arrival workload is submitted with the same two exact 1.5B resident targets, actual 4,000-token prompts, concurrency one per target, maximum eight output tokens, 900-second baseline and mixed windows, and unchanged 1.5-second gate. This quadruples the declared request rate and keeps control connections active; it does not discard failed samples, weaken the percentile/sample gate or relabel failed profiles. The dedicated development key has 9.039 million remaining tokens, enough for the declared ~7.2-million-token experiment. Qualification, if successful, is restricted to the actually recorded workload, with live latency guards still required for ordinary admission.

### Measurement event-loop regression — 2026-10-07 04:02 UTC

The measurement watchdog synchronously read its SQLite journal and observed native
processes on the request event loop. A bounded occupied-journal regression failed
before the fix: another coroutine could not run until the blocked journal returned.
Journal reads, probe/status observation and death-proven release now use the
existing executor; the lease endpoint also offloads status observation. Six
measurement-supervisor tests and three measurement-route/SDK tests pass. T137
remains open pending immutable packaging and native validation. The ongoing B
coexistence experiment d28f7bea-cc03-45a8-aef7-dbaf88fa13c3 retains its original
runtime; no latency cause or passing profile is inferred from this unit regression.

Lease renewal now executes its unchanged durable transaction in the executor
under the existing command lock. A second regression proves concurrent request
responsiveness and that cancellation retains serialization until the write
finishes. Seven supervisor tests and the authenticated measurement-route test
pass together (8 passed); both SDK tests passed separately. No active node runtime
has been modified by these source changes.

The renewal correction additionally passes all 79 existing lifecycle, native
registration and lease-snapshot tests in 2.54 seconds. Ruff formatting/checks
pass on the changed files. Native validation and immutable activation remain
pending; the active coexistence trial is deliberately untouched.

### Two-second coexistence baseline — actual failure retained

Experiment `d28f7bea-cc03-45a8-aef7-dbaf88fa13c3` completed the full
900-second baseline from 03:55:21.856413Z to 04:10:21.858386Z. Each exact
resident completed 444 requests. Twelve scheduled arrivals encountered their
occupied concurrency-one slots (six per target); this correctly made the
baseline unsafe and prevented training start. No updates, mixed phase or profile
were produced. The command ledger retains the full baseline, although the
terminal report fallback does not expose its phases. The observer now retains
that phase privately alongside all failed requests/receipts/results. Metrics:

```json
{
  "started_at": "2026-10-07T03:55:21.856413Z",
  "finished_at": "2026-10-07T04:10:21.858386Z",
  "failures": 12,
  "targets": {
    "1297291a-54be-48df-ab74-4471ae0179bc": {
      "completed": 444,
      "p95_seconds": 0.599700083001153,
      "max_seconds": 3.0150058540002647
    },
    "db5cfaa7-cd72-4d18-8c5a-5adfbff66b69": {
      "completed": 444,
      "p95_seconds": 0.5902619469998172,
      "max_seconds": 2.995284996999544
    }
  }
}
```

The next separately frozen workload uses 3.5-second arrivals to avoid arrivals
overlapping the observed three-second tails. Both phases must still use the same
rate, full 900-second windows, >=100 requests/target, <=1.5-second p95, positive
training updates and zero swap. No previously failed sample is removed or
reclassified. Native event-loop corrections remain a separately proven defect.

### Immutable request-loop/runtime activation — 2026-10-07 04:16 UTC

Both nodes now use `/opt/coire/envs/0.2.0-4d7af471136a`; activation checked
all native journals for no running training/measurement owner, verified the
existing launchd agent UID/PID/birth, atomically updated the runtime link and
reloaded only that agent. Authenticated health and digest-pinned harness images
passed on both nodes; the two exact B resident engines re-adopted and returned
READY. Private activation receipts retain previous environment and process IDs.

The frozen build passes 723 node tests (one existing non-Darwin fallback skip,
35 engine-marker deselections), 20 actual tiny-model engine tests on A in
31.40 seconds, and both fresh-process SDK tests on that exact A environment
(2 passed, 0.84 seconds). Strict mypy passes all four changed source modules.
No numerical function changed: native `execute_native` AST SHA is identical
`22ba18502bc12fdfe4fb8cd2cc4700a1fe4ca1a781cf13db869e8c70e1cbfea3`;
`native_measurement` is identical
`9ab5dd76a1bfc733a09ce255834bcc4b963a3cd1589cd56dd7af9a7fcbe7d7ca`.
Runtime identity remains the frozen MLX/MLX-LM/transformers/tokenizers version
digest, and the full deployment identity is recorded separately rather than
rewriting earlier memory-pilot evidence.

Fresh experiment `f48a7722-a3d5-4c58-868d-61d7c99b1f40` uses 3.5-second
arrivals, the same exact resident pair, 4,000 actual tokens and concurrency one
per target. Its result remains pending. A separate 12-update A job validates
ordinary-worker parent attribution after the source fix. T134/T137 are not
claimed complete before their required live verification.

Actual new-runtime job `01M4A9AN0J1PCRXKFEDQ783FKG` completed 12 updates,
committed replicated checkpoints and served its adapter. Tempo trace
`a5e6af284d464b6015a1041c9cf25ebf` contains 46 native spans, all descending
from `coire.node.training.worker`, with exact job/attempt/node attribution.
Load, render, preflight and execute children were verified from the trace
parent graph; this closes the confirmed ordinary-worker attribution defect.
T134 still retains its pending current-runtime mixed-profile verification.

### Final baseline and credential prerequisite — 2026-10-07 04:39 UTC

Current-runtime experiment `f48a7722-a3d5-4c58-868d-61d7c99b1f40`
completed a full baseline but retained 14 failed arrivals and therefore stopped
before training. No mixed result or profile is enabled. Its exact retained
baseline is archived privately with request, receipt and terminal result:

```json
{
  "started_at": "2026-10-07T04:17:07.133524Z",
  "finished_at": "2026-10-07T04:32:10.135855Z",
  "failures": 14,
  "targets": {
    "1297291a-54be-48df-ab74-4471ae0179bc": {
      "completed": 251,
      "p95_seconds": 0.5791689899997436,
      "max_seconds": 3.237214247999873
    },
    "db5cfaa7-cd72-4d18-8c5a-5adfbff66b69": {
      "completed": 251,
      "p95_seconds": 0.5702775640002073,
      "max_seconds": 3.177765564998481
    }
  }
}
```

The available duration data does not yet distinguish concurrency collisions
from completion timeouts; do not claim a diagnosed live cause or simply declare
a slower workload passing. Node request-loop blocking remains independently
proven, corrected, gated and deployed.

The login Keychain is now locked: `SecKeychainGetStatus` returns flags 2 with
unlock-state false; CLI credential lookup exits 152, corresponding to
`errSecInDarkWake`. A bounded user-activity assertion did not unlock it. The
mounted compatibility administrator secret returned 401 and was not enabled or
used to bypass identity. Existing services retain their configured secrets.
User command needed on core: `security unlock-keychain
~/Library/Keychains/login.keychain-db`. No password should be sent to the agent.
After unlock, configure a separate ephemeral development Keychain with explicit
local-tool access and audited scoped credentials so later login-Keychain idle
locking does not interrupt testing. Keep it private and revoke temporary keys
after acceptance.

The separate image credential was created through the audited admin API and
stored in login Keychain: key `bc3f09a5-0b76-429c-b3e6-d00048a732af`,
service `coire-016-image-admission-key`, scopes admin/images. Catalog GET 200
returned the existing eligible Z-Image-Turbo asset. Its default Keychain ACL
blocked the image driver; the two owned setup processes were stopped before
any workload was submitted. Key revocation/replacement awaits authenticated
access. No image-generation or newer-pin acceptance is claimed.

Prepared private drivers: `scheduler-restart.py` refuses any active measurement
before restarting only the scheduler; `train-first-image-pin-live.py` awaits
credential access and has submitted no training/image job. The root link helper
is still unarmed, with no trigger or receipt. Its one-shot command remains
`ssh -tt mcteer@coire-edge-a 'sudo /opt/coire/envs/current/bin/python3
/Users/mcteer/coire-stage/016-link-fault/link-fault.py --arm'`. Runtime
activation needs no additional sudo. Feature acceptance remains incomplete.


### 2026-10-07: credential recovery completed; physical link helper armed

The operator armed the bounded Studio A link helper at 13:31:30 UTC; its
three-hour wait ends at 16:31:30 UTC. It has not yet been triggered.
The login-Keychain unlock attempt did not provide usable unattended reads.
Under the operator’s explicit authorization to configure test credentials,
the documented identity incident-recovery bridge was enabled for key issuance
only from 13:39:28.005433 to 13:39:32.543285 UTC (4.537852 seconds), with an
independent 180-second restore watchdog. Normal configuration was restored
before workloads; the compatibility bearer then returned 401 and the issued
scoped key returned 200. ADR 0013 records this ended Constitution IV exception.

Audited API issuance created development key
989d3fcd-bb22-4a4c-835a-031714fcdc57 (admin/chat/mcp, RPM 600, 20M monthly tokens)
and image-admission key ceac98f0-4494-4259-8d4e-1f6e04fe329c
(admin/images, RPM 600, 100k monthly tokens). Secrets reside in the separate
0600 encrypted private acceptance Keychain, with explicit local-reader access;
the user’s login Keychain and default search list were preserved. Private
acceptance scripts now address that Keychain explicitly. Proof:
`evidence/credential-recovery-live.json`. Earlier credential-blocked notes above
are historical; no further unlock or sudo is required for the pending tests.


### 2026-10-07: scheduler restart and bounded physical link fault passed

Scheduler restart job 01M4B9RBS51XJ1Q4MV699W4KXK retained native trainer
PID 15476 with birth time 1791380818.774178 through a 2.322-second scheduler
restart, completed 12 updates, mirrored full checkpoints 4/8/12 and served
adapter 744cfdcb-eeb1-5de1-b4b0-9a44844c5c1c. The completed owned A fixture
was explicitly drained through the audited API to obtain isolated admission.
Private proof: `evidence/scheduler-restart-live.json`.

Physical link job 01M4BA4GXD1D7MDCNVDCCRM57N triggered the operator-armed
helper after committed checkpoint f9d0db10-4741-5411-892d-9afa9af0fc99.
Studio A en5 was confirmed down at Unix 1791381121.734092 and restored up at
1791381151.764433 (30.030341 seconds), with an independent 45-second restore
watchdog. The controller observed rank_failed, fenced attempt
01M4BA4HD8S7EPK09K8DHAW9JB/fence 1, recovered under fence 2 from a newer full
committed checkpoint 8, completed 12 updates and passed independent mirrored
checkpoint/adapter-serving checks. Private proof: `evidence/physical-link-live.json`.
The one-shot helper has completed and requires no further operator action.
An earlier observer exceeded its RPM 600 credential limit and retained an
unresolved cleanup result; it injected no fault. The successful observer uses
0.5-second polling, preserving authentication limits. That prior job’s finalizing
state is being reconciled separately; no success is inferred from its partial
report. Transfer corruption/grant-refresh acceptance remains separate.


### 2026-10-07: strict workload failure diagnostics and idle-eviction defects

New bounded diagnostics distinguish occupied concurrency, completion timeout,
identity mismatch, operation error and arrival cap without prompt/exception text.
Seventeen measurement unit tests, Ruff and strict mypy passed. API image
f566208f17fc66cae995a92a2ed544e98c4eb37cd47c5467c4181da16482ae53 and
scheduler 769038462bce85b140bd8efd79a9c2a41d863889e88b766d3a83628bd6686c74
passed policy rules, CRITICAL vulnerability/secret scans and SPDX generation.
The jobs dashboard and operational runbook explain the failure counter.

Experiment e3f7017f-8b99-4cf3-8a93-bdc2cece5f3b used fresh exact B residents
33f4c3cc-6cfd-457c-8778-36e2089d415c (pinned ops base) and
f9d337b3-5f15-4c27-be6b-92b7e425dfe8 (verified adapter). The same actual
4000-token prompt was attributed to the new identities and its canonical digest
refrozen; fixed arrivals were 5000 ms, concurrency one, output eight. The full
baseline remained inconclusive, with no mixed updates or profile approval.
The live failure counter reports completion_error, and trace
eb6dee2f8fb4f630aa537a3015bffbc5 records actual httpx.RemoteProtocolError:
server disconnected before response headers. This distinguishes the failures
from occupied slots and completion deadlines. A shorter idle connection reuse
policy is a hypothesis being gated and tested in T140; no passing comparison
is claimed yet. The TCP regression passes for replacing idle connections while
the default pool reproduces the disconnected-peer error. Gateway proxy suite:
12 passed.

The train-first image/newer-pin trial reached admission but exposed two defects:
ordinary placement tried to parse training-drain as a load policy, and the
controller probed absent native status before the drain barrier created a
preparation. Fixes have 22 passing Postgres admission tests, including both new
regressions. A stop before preparation now persists typed node no-start proof
that survives restart and blocks late prepare; three node rejection tests pass.
These fixes are not yet deployed or accepted on hardware. Trial
01M4BAJJN3TTP36B3MW5XZTDXM is cancelling with zero updates; counted ownership
remains until actual proof. Its temporary idle policy and pin were restored
through authenticated optimistic-concurrency mutations (200/204).
Old blocked image test key bc3f09a5-0b76-429c-b3e6-d00048a732af was revoked
through the audited admin API (204). Earlier rate-limited link-observer job
01M4BA0E5MGATK2KDZG4YT0W0X is also cancelled after explicit cleanup.


### 2026-10-07: idle-drain and no-start proof corrections deployed

The complete local node suite passes 724 tests (one unchanged platform fallback
skip, 35 engine-marker deselections), in 41.01 seconds. The immutable candidate
/opt/coire/envs/0.2.0-083dc41720b1 passed 19 actual tiny-model Studio A tests
with no skips in 31.21 seconds, plus both actual worker telemetry SDK tests in
1.86 seconds. Both Studios were activated after native measurements stopped;
authenticated health, paired run-image pins and owned engine re-adoption passed.

The API image 05223944ae8e4c603b859069b89360917e78f5a57591465130383e81054dde66
and scheduler d3dd9a232bc5772ed9f0bc355b34c5aaeb12927e6a5e1a37f527370a1eeef82a
passed all image policy rules, CRITICAL vulnerability/secret scans and SPDX
generation, then were deployed. Ruff and strict mypy for the changed modules
passed. Ordinary placement excludes training-drain; the controller defers native
observation until preparation exists or is dispatched. The node persists a
pristine pre-prepare stop tombstone, scoped to the typed command, after process
inventory proves absence. The same attempt cannot prepare/start later; changed
fence and uncertain ownership remain refused.

Job 01M4BAJJN3TTP36B3MW5XZTDXM reconciled to cancelled at zero updates with
actual native stop proof after activation, retaining no adapter. Private proof:
`evidence/unprepared-attempt-stop-live.json`. A fresh A fixture
49dde336-8240-40b3-a3cf-fba65d0074b5 serves the link-test adapter for the repeat
eviction/image/newer-pin trial. No current-runtime mixed profile is approved yet.
Experiment 1764fdba-983d-47e5-a2ab-f10479dd399c repeats the frozen 5000-ms
workload with conservative one-second idle upstream reuse. Its full real
baseline/mixed acceptance is ongoing; do not treat an early window as success.


### 2026-10-07: live corrected idle eviction, newer pin and train-first image passed

Job 01M4BBSJV9A1F4BHG5T1VTCP07 completed 12 updates with full mirrored checkpoints 4/8/12 and
served adapter 2dd97ba7-e086-5682-97fd-c08eba4a1762. Its owned A fixture
49dde336-8240-40b3-a3cf-fba65d0074b5 was actually evicted. An authenticated
newer pin on the released victim reservation made restoration superseded;
the original instance remained stopped. Fifteen repeated live observations
showed the submitted image queued with zero execution leases while training
owned A and the measurement owned B. The image was cancelled through its own
API. Idle TTL and pin cleanup both succeeded (200/204). No image output is
claimed by this train-first exclusion trial; reverse exclusion is being tested
with a genuine image generation separately.


### 2026-10-07: reverse image/training exclusion passed

Real image job 01M4BC4PJBQCVKPEPZWTJ4V95N completed the existing registry-
verified Z-Image-Turbo 512×512 four-step workload on Studio A. While its image
reservation was counted, twelve live training observations retained queued/
preflighting, zero updates and no native attempt, including capacity_busy.
The queued training job was cancelled through its authenticated, versioned
control API. Private proof: `evidence/image-first-training-live.json`.
No new asset was acquired. The image worker retains its normal idle lifetime;
no API-key impersonation of a human-only unload route was used.
An earlier test observer used a stale expected version at cancellation and
retained an HTTP error; its image/training jobs were cancelled through normal
cleanup. The successful repeat refetched the current version before control.

The short idle-pool experiment 1764fdba-983d-47e5-a2ab-f10479dd399c remained
inconclusive. It ended before a complete phase could be retained; no profile
is approved. Its workload failure counter is absent, and usage records include
one failed request following phase cancellation. Do not claim a full latency
or idle-pool causal improvement from this short result. A simultaneous A
admission test occurred during its early baseline; repeat with other workloads
quiet before making supported-mix claims.

Credential audit rows independently confirm API key issuance at 13:39:30.949090
and 13:39:31.019182 UTC, and old image-key revocation at 13:56:44.253404 UTC.
The quiet experiment 1fe45744-5a4f-4e07-8dad-b3e5967ed4cd follows twelve
actual 4000-token warm-up streams, all completed, with first tokens 0.259–0.514 s.
Warm-ups are private diagnostics and do not count toward qualifying windows.

### Quiet coexistence rerun and request-loop regression (2026-10-07)

The unchanged 5000 ms workload on measurement `1fe45744-5a4f-4e07-8dad-b3e5967ed4cd` stopped inconclusive before completing its baseline. The late rolling window contained 55 samples per resident, with p95 first-token latency 1766.3 ms for the base and 1760.1 ms for the adapter, despite earlier p95 near 600 ms. No mixed phase or approved capability profile resulted. Both residents experienced multi-second stalls; their cause remains unproven.

T141 moves ordinary watchdog journal reads and exact engine/adapter ownership lookups to worker threads, preserving the shared lock and validation. Node proxy lookup, adapter, stream and bare-upstream header spans distinguish ownership waits from model response waits. The focused responsiveness/engine contracts passed 30 tests; the first broad run found an existing direct-call contract missing the newly injected HTTP request argument. The contract now supplies an actual Request; the corrected full run and immutable Studio gates are pending. The candidate remains staged without activation until those gates pass.

### Request-loop candidate activation and diagnostic backend recovery

Candidate `/opt/coire/envs/0.2.0-472733ecc21a` passed 726 node tests (one existing platform-specific fallback skip), 19 real tiny-model native tests without skips, two SDK entrypoint tests, Ruff and strict source mypy. Both Studios activated it and passed authenticated health with configured harness images; B re-adopted both resident engines. An independent diagnostic run still observed shared multi-second delays, so the lock changes are not claimed to have closed sustained coexistence.

Docker reported Tempo `OOMKilled=true` and 211 restarts under its original 512 MiB ceiling. The diagnostics-only service was recreated with a fixed 1 GiB ceiling and a 768 MiB Go runtime budget, preserving the trace volume and all hardening. Subsequent exact trace reads and readiness succeeded with zero new restarts so far. Final sustained verification remains pending. The API now propagates standard trace context only on authenticated native node proxy requests; its 13 gateway proxy regressions, Ruff, strict source mypy, image policy, CRITICAL vulnerability/secret scan and SPDX generation passed. Image `sha256:473c28bbe0acfdea584910eb7c082c261551e436e94d98fd8170af6cd03f7008` is active. A non-qualifying diagnostic observer was interrupted by that API replacement; its log is retained and no formal phase/profile is claimed.

The first isolated grant-refresh trial exposed a real status race: the 202 refresh response and immediate polling still reported the previous terminal failure while a new transfer task was scheduled. The existing unit test also observed that stale failure rather than the second receiver checksum result. The importer now durably records staging with a cleared reason before scheduling refresh; the regression asserts the fresh staging response and subsequent `replication_failed` digest rejection. Seven import/range/route tests pass. The refreshed Studio runtime and repeat hardware evidence are pending; the failed isolated fixture was removed and no live artifact was changed.

Host and container health probes independently observed the same ~1.3-second stall, ruling out Docker-only transit as the explanation. Correlated trace `7a9f9efe0cc14e79a1c665303a05e718` contains ~1.94 seconds between core upstream start and native proxy lookup; native lookup took 0.68 ms and bare response headers 47.43 ms. Source inspection confirms each five-second registry reconciliation runs node process inventory synchronously under the shared ownership lock. A new regression blocks process inventory and requires both the event-loop heartbeat and unrelated engine snapshot to proceed. Reconciliation now runs in a thread and scans outside that lock, with concurrent ownership and exact live-process checks before adopting an orphan. The simulated orphan contract now covers both still-live and dead discoveries. Live causal comparison remains pending.

### Current frozen inventory/transfer runtime and real transfer faults

Candidate `/opt/coire/envs/0.2.0-511c4a911f79` passed 728 node tests (one existing platform-specific fallback skip), all 19 native tiny-model tests without skips, both SDK tests, Ruff and strict source mypy. Both Studios activated it and returned authenticated health, configured harness images and the two exact B resident engines. OpenAPI freshness passed.

The repeated isolated data-fabric transfer trials passed on this runtime. An expired grant failed with `lease_expired`; corrupted received bytes failed with `replication_failed`. Both incomplete artifacts refused verification with HTTP 409 and held no verified manifest. A fresh grant resumed each identical scoped import; receiver byte/digest verification succeeded for all 142566 bytes. Every grant was revoked and only the new private fixture directories were removed. Original committed checkpoint `8d864a87-3ade-5c21-a8ee-5f04d6a5163c` was unchanged. These are real authenticated native transfer-protocol tests, not a claim that a Core registry row was published or that the controller itself retried an injected fault. Mid-transfer interruption remains pending.

Tempo remained healthy with zero OOM/restarts for more than 15 minutes after recovery while diagnostic requests and exact trace retrieval continued. Its sampled resident use was 104.8 MiB within the fixed 1 GiB ceiling; proof is `tempo-memory-recovery-live.json`. T142 is complete.

### API restart and interrupted native replication acceptance

Actual API restart job `01M4BFX18RRCPVTTE3TA5PDK8G` preserved the same native trainer PID and birth identity across a measured 2.87785079-second restart, completed all 12 updates, mirrored checkpoints and served the resulting adapter. No duplicate trainer was created. This supplements the previously recorded scheduler and node-agent restart trials.

A real mid-transfer grant revocation on an isolated checkpoint copy stopped replication after 84028646 transferred bytes. Receiver verification returned HTTP 409 before publication; a fresh, identically scoped grant resumed the partial transfer and verified the complete 537013478 bytes. The fixture contained 512 MiB of synthetic padding distributed over separate files to permit deterministic interruption between authenticated file requests. It was never registered or loaded as a training/model artifact. Every grant and test-only source/receiver directory was cleaned up; committed artifacts were unchanged. The earlier API cancel trial finished its already-owned copy before releasing reservations and therefore did not establish interruption; that failed observer result is not counted. The successful authenticated grant-revocation case supplies the before-manifest-commit evidence.

The post-inventory diagnostic still observed multi-second arrivals. The retained 90-second native process sample contains 391 samples in gRPC WorkStealingThreadPool PrepareFork while Python held the GIL; the request thread simultaneously waited to reacquire it. OS metrics use fork-based ioreg subprocesses, so merely moving work to a Python thread cannot avoid this process-wide pause. T143 replaces those read-only OS probes with public Darwin posix_spawn and close-on-exec-by-default, explicit stdio, a credential-free environment, bounded capture and deadlines. A standalone SDK probe did not reproduce the production process stall (max 30.85 ms loop lag); the production stack sample remains the causal evidence and live post-deployment comparison is required.

### Native probe spawn runtime activated

Frozen candidate `/opt/coire/envs/0.2.0-d6a27507f84e` passed 732 node tests (one existing platform fallback skip), 23 real Studio tests (the 19 native tiny-model cases plus four native probe tests), both SDK cases, Ruff and strict source mypy. Both Studios activated it and passed authenticated health with configured harness images; B retained both exact resident engines. No dependencies, network settings or telemetry exporters were changed. The three isolated transfer-fault artifact IDs were independently confirmed to have zero Core checkpoint rows. T080 and T110 now have the required restart and rank/link/replication evidence; full coexistence and remaining guard/rollback acceptance remain open.


### Sustained unchanged workload qualification passed (2026-10-07 UTC)

Measurement `fe31b6b3-a52d-4f9c-b9a8-8eaa30c1bbb1` succeeded and created profile
`dcc68261-a9ce-46e9-b59d-cbd894bc8dff`, valid until 2026-10-14T16:22:44.825014Z.
Both Studios use frozen node environment `0.2.0-d6a27507f84e`; API image
`473c28bbe0acfdea584910eb7c082c261551e436e94d98fd8170af6cd03f7008` and scheduler
`d3dd9a232bc5772ed9f0bc355b34c5aaeb12927e6a5e1a37f527370a1eeef82a` remain active.
No arrival, prompt, failure, p95, duration or numerical training criterion was loosened.
The same frozen 4,000-token prompts, 5,000 ms arrivals, eight output tokens and one
concurrent request per target were used for both phases, including the pinned ops
base resident and exact verified-adapter resident on Studio B.

| Exact resident instance | Baseline completions / p95 seconds | Mixed completions / p95 seconds |
| --- | --- | --- |
| `33f4c3cc-6cfd-457c-8778-36e2089d415c` | 180 / 0.533084 | 180 / 0.472588 |
| `f9d337b3-5f15-4c27-be6b-92b7e425dfe8` | 180 / 0.538328 | 180 / 0.486522 |

Baseline: 15:48:38.059351–16:03:38.062071 UTC; mixed:
16:03:45.165606–16:18:45.167286 UTC. Both windows exceed 900 seconds and have
**zero failed requests**. The trainer remained active throughout mixed and completed
all 16,384 updates; peak measured native footprint 1,419,544,232 bytes, zero swap
growth, thermal nominal/acceptable. Report SHA-256:
`34af83cbb35b133e19adedd608f7478f1a6423eb263509069320389b8b60a087`.
Configuration SHA-256 `789d689e1265371ca0784c27928d96e70ea03bad3941be0e59f229139be5c075`;
numerical runtime `a44ee653923d49469382a0c840429e7ec1f9c630db187ef2323babfbc0f0099a`.
Private request/report and full retained phase arrays are `coexistence-native-spawn-live-*`.
Previous failed/inconclusive measurements remain unchanged and approve no profile.

Tempo exact trace `99b45b0a641cca3dd40a4b0756f2f8f2` contains the owned measurement
parent, nested execution, two load stages, two preflight stages, 40 render stages and
16 serialization stages. Metadata-only stage/parent evidence is retained in
`coexistence-native-spawn-trace.json`; ordinary worker and CPU-analysis attribution
were separately proven earlier. Tempo remains healthy, zero restarts and no new OOM
since its bounded recreation. This closes T111/T134/T140/T141/T143. The comparison
supports the combined transport/request-loop/native-probe correction; it does not
claim the speculative HTTP idle race alone explained all previous failures.


### Ordinary mixed admission and injected protective guards passed

Ordinary job `01M4BJSY941T3P4XJ54M2HXXEG` reused the exact qualifying training
configuration and Studio-B residents. It initially queued `insufficient_samples`
while the previous rolling window aged; fresh identical chat requests restored the
30-sample/freshness guard without changing the profile or telemetry requirements.
It then ran 25 positive updates and authenticated cancellation completed in
1.652914 seconds, no adapter. `ordinary-mixed-live.json` retains the result and
metadata-only contemporaneous chat samples. This closes ordinary acceptance T136.

The acceptance driver uses `AcceptanceRunner` from `scripts/validate-sft-training.py`
for authenticated submission/current-version cancellation. Each protective trial
copies control metadata into its own disposable `coire_016_guard_<uuid>` database,
asserts this namespace before any mutation, and runs the production guard/controller
against that isolated copy. Only observed adverse values are injected there; production
ledger health, usage and profiles are never fabricated. A restricted transport permits
only one exact test-job/attempt/fence/node/rank pause command through normal node
authentication. It forbids start, lease, extraction and publication. Real node events
then inform the canonical controller of protective pause, and actual stop proof precedes
hold release. These are injected-condition tests, not claims of physically overheating
or exhausting either Studio.

| Condition | Test job | Real protected stop seconds | Final counted training holds |
| --- | --- | --- | --- |
| Serious thermal reading | `01M4BJWWR87J52M9JJJ068R34R` | 1.856740 | 0 |
| Footprint exceeds reservation | `01M4BJXXX99VPY3JYC34T6EX9Q` | 2.328538 | 0 |
| Thirty fresh 2-second latency samples | `01M4BJYXSYW87Z50BN0RQ1CV9A` | 2.313972 | 0 |

The stale 61-second health observation generated no pause command and left the
trainer running; existing real-Postgres contracts cover stale new-admission refusal,
also independently observed by the ordinary mixed queue. Latency classification
invalidated only the isolated execution-matching profile. The real qualified Studio-B
profile was not invalidated using synthetic observations. All trials were cancelled
through current-version admin API controls, published no adapter, and all disposable
databases were dropped. No production database hold was manually released. Private
`protective-guards-live.json` retains native stop statuses, controller command receipts,
durable events and cleanup results. Together with both image/admission directions,
newer-pin-aware restoration and previous real outage/stall faults, this closes
T112–T114 and the remaining T122 guard/trace evidence. T123 final rollback and final
packaging/static gates remain open.


### Disabled drain, preserved stores and compatible rollback passed

Active job `01M4BK3H747RVYBAZ2GGMZGR5B` had committed update 4 when compatible
API/scheduler were recreated with `TRAINING_ENABLED=false`. The disabled controller
correctly cancels active/finalizing work through owned stop commands; it automatically
completed before the observer's expected running-state read. Initial observer assertion
failure is retained, not represented as a passing cancellation request. Read-only recovery
verified durable `cancelling` at 16:28:50.844453 UTC and `cancelled` at
16:28:51.044127 UTC: **0.199674 seconds**, zero counted holds, no adapter, retained
complete checkpoint verified on both Studios. New validation returned 503; history and
checkpoints returned authenticated 200. Cancel on the already-terminal job returned the
expected 409 rather than a feature-disabled 503. Paused jobs with no active trainer are
preserved by the separate disabled lifecycle branch. `disable-drain-live.json` contains
these durable observations; no database cleanup or manual release was used.

After every job/measurement was terminal, actual compatible rollback changed API
`473c28bbe0ac…` to previously gated `05223944ae8e…` and Studio A frozen environment
`0.2.0-d6a27507f84e` to `0.2.0-511c4a911f79`. Schema 0031, settings, digest-pinned
harness images, checkpoints, adapters and audit remained retained. Rollback took
15.091312 seconds; restoring the tested current API/node took 14.488141 seconds.
Authenticated readiness, retained job/checkpoint identity, native SHA/byte verification,
base gateway chat and exact verified-adapter chat all passed in **both** phases.
Studio B's residents were untouched. Training admission remains disabled during final
gates. Metadata-only `compatible-rollback-live.json` retains image/environment and
process-birth evidence. This is not a destructive production schema downgrade.

Complete node-owned artifact-store archives were created privately on **each Studio**
and restored into isolated temporary directories there. Every original/store/restored
file digest matched; original stores were not changed. A: 16,947,200 archive bytes,
782 files, **146/146 complete manifests verified**. B: 16,343,040 archive bytes,
759 files, **140/140 complete manifests verified**. Restore directories were removed;
archives remain 0600 under each Studio's `~/.coire/016-backups/`. Metadata-only
`coire-edge-{a,b}-full-artifact-backup.json` records archive SHA and location. No tensor
bytes were copied to Core. Prior dataset restore, real metadata restore, nine migration
refusal/clean downgrade-upgrade tests remain valid; fresh metadata preservation and
final release gates follow.


### Final code gates and external deployment boundary (2026-10-07 UTC)

Final corrected local unit/contract selection: **2,601 passed, 2 skipped, 408
integration/engine cases deselected**, 100.89 seconds. Enabled real Studio gate:
**23 passed, no skips**; separate native SDK entrypoint gates passed. Isolated
Postgres feature integration: **162 passed, no skips**, 193.25 seconds. Web:
**171 passed**, lint/build green; final runbook renderer **2 passed**. Ruff checks
**1,522 files**; strict mypy **806 source files**, OpenAPI freshness and byte-equal
regenerated TypeScript schema pass. Hardened Prometheus rule test passes with its
required private `/tmp` filesystem. An initial test invocation omitted that mount
and was corrected; no production rule or image hardening was weakened.

The first broad Python run failed the two native SDK export tests because the API
session fixture sets `OTEL_SDK_DISABLED=true`, inherited by their fresh subprocesses.
Only the export-test children now explicitly enable the SDK, retaining in-memory
exporters. Both passed with a deliberately disabled parent; the complete broad
suite then passed. Four focused test type errors were also corrected with typed
SecretStr/ChatMessage, getattr on structured LogRecord attributes and direct psutil
module patching. No production numerical or telemetry configuration changed.

Final web image `22a62c1ba1c068c60b6849d6d80419aa73eac6a4de275cc4d56814623b704164`
and Grafana `caea5a9a4d87ee228bfaaf67bcf6b9a48c5ae184af3aeb7a47f15cce60f5b329`
built and passed all seven policy rules, zero CRITICAL vulnerability/secret findings
and SPDX generation. Fresh Core images built successfully: API `41d34f457bb52efe…`,
scheduler `ae0345d896e4883a…`, MCP `d05456309b695a27…`, migrate `c96b6eec7441d650…`.
The session then changed to workspace-write/restricted networking with approval
policy never. Docker access now returns **permission denied** for
`/Users/mcteer/.orbstack/run/docker.sock`. Consequently these four fresh images'
final gates/deployment and fresh final metadata restore remain unexecuted. Existing
qualified API/node builds remain installed; admission is disabled. All actual
runtime acceptance remains complete and preserved.

A concrete operator completion helper is staged at
`/private/tmp/coire-016-finish/complete-release.py`; its offline prerequisites, syntax
and Ruff checks pass. It checks exact built identities, all image gates and source
equivalence to the qualified API before deployment, verifies no active trainers,
retains current node/agent pins and restored original ops settings, activates the
images, verifies authenticated routes/health, revokes temporary acceptance keys,
restores a fresh metadata dump only in a disposable named database, and records
private step receipts. Only after success does it close T121/T123/T124 and final
spec/handoff status. No sudo or login-Keychain change is needed. The raw secrets
are never printed or supplied in shell arguments. User-terminal execution is the
remaining concrete prerequisite; the sandbox boundary is not bypassed.

Every FR-001–FR-036 and SC-001–SC-012 mapped task was rechecked against the coverage
table and recorded tests/runtime evidence. All mapped implementation tasks are
checked; only the final three release tasks remain unchecked. Handoff rewritten
to the PR template with explicit dependencies/licences, Constitution I–VII,
ADR 0013's ended credential exception, exact supported matrix and current boundary.
No commit, push or PR was created.


### Final qualified release completion

Operator completion passed all image gates, source equivalence, authenticated service/node checks, fresh private metadata restore and acceptance-key revocation. Development training is enabled; configuration defaults remain off. Original ops settings are restored and temporary residents/pins removed. No production schema downgrade or Git commit/push/PR occurred.

- `api` image: `sha256:41d34f457bb52efea6cbefd0e1c7c8b3b960043d25a4ea6eb7902a05d6e90e59`; all seven policy rules, zero CRITICAL vulnerabilities/secrets, SPDX generated.
- `scheduler` image: `sha256:ae0345d896e4883ad5c5167a8fbcd58d57c1c35177c8966e4b1e8cf038ee7a1f`; all seven policy rules, zero CRITICAL vulnerabilities/secrets, SPDX generated.
- `mcp` image: `sha256:d05456309b695a274edf1bd99e0692223fa15f4dbd624a3647a0b16be1bc4587`; all seven policy rules, zero CRITICAL vulnerabilities/secrets, SPDX generated.
- `migrate` image: `sha256:c96b6eec7441d650591ddd81b045b50bdb560d213228e97e413c2dc2277fa269`; all seven policy rules, zero CRITICAL vulnerabilities/secrets, SPDX generated.
- `web` image: `sha256:22a62c1ba1c068c60b6849d6d80419aa73eac6a4de275cc4d56814623b704164`; all seven policy rules, zero CRITICAL vulnerabilities/secrets, SPDX generated.
- `grafana` image: `sha256:caea5a9a4d87ee228bfaaf67bcf6b9a48c5ae184af3aeb7a47f15cce60f5b329`; all seven policy rules, zero CRITICAL vulnerabilities/secrets, SPDX generated.

Private proof: `qualified-release-final.json`; restored metadata counts and archive SHA are included there. T121/T123/T124 are complete.


### PR 91 Linux CI corrections (2026-10-07)

Run 37659792777 exposed two host-dependent validation issues: Linux has no
installed MLX callback class for strict subclass checking, and the measurement
route contract assumed Darwin/arm64 plus an available checkpoint hook. Added a
minimal pinned upstream callback type stub for static checking without importing
MLX, and explicit platform/hook fixtures in the contract. The same contract now
asserts Linux returns no supported world sizes. Runtime code and admission gates
are unchanged (Constitution I, II, VII).

Ruff format/check passed (1,523 files); strict mypy passed all 806 files, including
a fresh Linux-platform check. The exact CI Python selection
`uv run pytest -m "not integration" -q` passed: 2,601 passed, 37 skipped, 373
deselected in 99.19 seconds. This selection includes unavailable engine/runtime
cases; the completed real Studio gates remain recorded above. The focused
measurement route contract passed. GitHub checks must pass on the pushed fix
before squash merge; no check or branch protection is relaxed.
