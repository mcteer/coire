# Quickstart: Control-Plane Failover Qualification

**Owner steering 2026-10-15:** Cloudflare stays deferred while local implementation,
validation and first local platform use take priority. Do not request account/tunnel setup
or treat it as a blocker to local work. Public acceptance below remains future, unclaimed work.

**Prepared**: 2026-10-14. **Scope amendment 2026-10-15**: owner deferred Cloudflare installation
and provider/public-edge qualification until last; origin/Studio checks continue. This is the validation procedure for the next implementation pass, not
an execution record. Record actual outcomes in [review.md](review.md). Historical September results
remain historical; do not replace missing evidence with task checkmarks or fabricated measurements.

## Preconditions and stop conditions

1. Use `feat/020-control-plane-failover` based on merged 019. Complete the corrective tasks before
   release qualification. Record source commit and digest-pinned images, schema/protocol version,
   membership epoch and effective timing/resource limits.
2. Inspect all three members through documented authenticated status paths. Verify healthy baseline,
   declared VLAN DNS/ports, no use of core Thunderbolt, standing 256 MiB reservations, no unresolved
   maintenance owner, and valid signed snapshot/identity authority on both Studios.
3. Confirm Keychain-backed independent peer, relay, monitor, lifecycle and operator credentials.
   Never print secret values or place them in shell history, fixture logs, Git, or this record.
4. Verify existing Cloudflare LB entitlement, stable Access hostname, three distinct outbound tunnels,
   supported ordered-pool policy, authenticated monitors, and an explicitly non-serving fallback.
   Read actual account configuration and measure propagation; template syntax is insufficient.
5. Confirm the approved failover image is precreated through `deploy/compose/coire-studio-failover-create`
   and the node owns activation. Do not start engines with shell commands or use caller-provided paths.
6. Obtain published, unrestricted, registry-approved resident test models through normal admin paths.
   Isolated CI uses `COIRE_TEST_MODEL` <=1 GB and never targets real Studios. Live acceptance may use
   existing verified lab assets under AGENTS.md authorisation. Scope must match FR-013.
7. Prepare an independently verified recovery route for Core power/network fault tests, a bounded
   reversible fault procedure, and the approved independent-origin fencing method for break-glass.
   Verify a fresh snapshot and sufficient remaining authority before **each** trial.

A missing credential, account capability, asset, recovery mechanism or required network approval
blocks only the dependent live case. Identify it precisely, continue independent work, and leave
that acceptance task open. Do not widen firewall, network, CORS, capabilities or Access scope to make
a test pass. Agent-run Studio acceptance is authorised; CI access to Studios is prohibited.

## Local and isolated validation

Run from the repository root after corrective implementation. Use existing locked dependencies.

```bash
uv run pytest -q packages/coire-core/tests/test_failover_models.py packages/coire-core/tests/test_failover_poller.py apps/coire-failover/tests apps/coire-node/tests/contract/test_node_failover.py
uv run pytest -q apps/coire-api/tests/unit apps/coire-node/tests/unit -k failover
uv run pytest -q tests/integration/test_control_plane_failover.py -m integration
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
uv run ruff check
uv run mypy
uv run python -m coire_api.openapi --check
uv run python -m coire_failover.openapi --check
```

Include the new restart/split-brain, middleware-maintenance, rate-limit, stream-expiry, journal fault,
monitor-scope and real tiny-engine tests listed in tasks.md. Inspect pytest collection and marker
requirements before selecting new cases; a deselected/skipped required case is not a passing gate.
Run the repository's required image/build/scan/SBOM, schema/type and alert gates once the integrated
change is stable. Re-run failed/changed areas as needed rather than repeatedly rerunning everything.

## Independent fault matrix

Use monotonic timestamps and an independent request/health observer. Record the authoritative host,
proof/epoch identity, public request result, live engine identities, maintenance owner, actual swap
counter, and audit event/receipt IDs. Redact identity and secrets. Restore a healthy fresh-snapshot
baseline between cases; do not extend the snapshot expiry to make a longer trial succeed.

| Case | Action | Required result |
|---|---|---|
| Baseline | Send authenticated browser/model/completion requests and negative requests to every route/origin. | Core alone admits; absent/wrong identity is refused. Monitor and lifecycle credentials cannot infer; user credentials cannot control peers. |
| Core loss | Make Core unreachable using the prepared reversible fault. | Edge-a alone admits supported resident inference through the public hostname within 60 s; record detection/election/start/monitor phases. |
| Snapshot horizon | Keep Core down beyond the remaining signed freshness window. | Readiness/inference fail closed at expiry, including break-glass; no local renewal or identity fallback. |
| Membership partitions | Test each isolated member and each majority side, including Core alive but isolated. | No automatic minority serving; expired proofs fail immediately; fencing within remaining lease plus 2 s evaluation. Direct/stale-origin traffic is refused too. |
| Restart and clocks | Restart a voter/leader with a live grant; test older TTL, missing/corrupt record, epoch change and clock jumps in isolation. | No overlapping admission; conservative guard persists; unsafe clock state fences. Replayed signed heartbeat does not prove liveness. |
| Lone survivor | Make Core and edge-a unavailable. Deliver a pre-authorised edge-b override after independent public-origin fencing. | No automatic promotion; only intended host serves until earliest authority expiry. Wrong target/epoch, replay, wrong kind, duplicate activation and missing fencing evidence are refused and audited. |
| Scope and limits | Send unavailable/nonresident/exact/unsupported targets, visual/tool inputs, overlong prompts and rate/concurrency bursts. | Stable refusal before engine work; no fallback, acquisition, placement, user harness, image or training action. |
| Sharded inference | With Core down and both Studios healthy, send a request to an already-resident approved sharded base model; then fail each rank in turn in a reversible isolated/live procedure. | Exact group serves while healthy; missing/failed rank interrupts within 2 s plus 1 s probe timeout; zero fallback, lifecycle or re-placement action. Record every rank identity and group generation. |
| Data link loss | Disconnect only the Studio data link while VLAN remains healthy. | Control election and supported single-node serving remain healthy; sharded admission/streams refuse or interrupt within 3 s; no control traffic shifts to Thunderbolt. |
| Recovery and streams | Restore authoritative Core during buffered and streaming requests; include long stream, disconnect, expired JWT/snapshot/proof and cancellation. | Admission stops before drain; only still-authorised work finishes within 30 s. Deadline/authority loss closes upstream work truthfully. Core admits only after fencing and becomes public within 90 s of continuous readiness. |
| Flapping | Produce ten Core healthy pulses each shorter than 45 s, then stable recovery. | Zero premature hand-backs, then one clean hand-back. Standing reservations remain. |
| Audit fault | Fill the unacknowledged journal; inject corruption/write failure and concurrent duplicate replay in isolation. | No silent loss; admission closes before exhaustion; reserved terminal record survives; safety fencing proceeds; acknowledged events reconcile exactly once. |
| Ingress fault | Make all pools unhealthy/unknown, stale-ready, draining, or credential-invalid. | Non-serving fallback; application and relay reject stale routing independently. No anonymous Access bypass. |
| 019 coexistence | Run Core upgrade success, failed-candidate rollback, and Studio environment transition with failover requests racing maintenance acquisition. | Exact writer/maintenance ownership and data preserved; no fabricated peer health; no forbidden lifecycle action; active streams remain inventoried. |
| Resource/telemetry | Compare a 15-minute idle baseline with failover armed, then exercise promotion alongside resident inference in lean and diagnostic profiles. | Incremental participant <=32 MiB RSS/1% CPU; frontend <=256 MiB; zero evictions and actual swap growth; real metric export, alerts and audit work without optional stores. |
| Rollback | Drain/disarm failover, restore compatible source artifacts via 019, and re-establish Core-only service. | No stale lease/override reactivates; audit/receipts and standing reservations remain; direct Studio origins refuse. |

Power/network interruptions are real-cluster acceptance only. Clock, corruption, pressure and replay
injection use isolated fixtures; do not tamper with production evidence stores or host clocks.
A live power-off trial needs demonstrated independent recovery; if none exists, record that exact
prerequisite rather than substituting a process stop and claiming power-loss acceptance.

## Evidence and closure

For each case record configuration, expected versus measured result, start/end times, artifact path,
source/image identity and cleanup outcome. Separate real public ingress, real model, composed ASGI,
and simulated-clock evidence. Every required failure must be fixed or explicitly remain open; do not
mark T036 done with skipped required tests. Preserve private raw evidence outside Git and commit only
redacted measurements and references. Runbook must explain how to observe, fence, kill and roll back.

## Operator-only real-host runners

`validation/participant_resources.py` runs signed production HTTP participants through private
SSH Unix sockets on Core and both Studios. The source tree under `/tmp/coire-020-qualification`
on each Studio must match Core; the runner checks runtime and fixture hashes. Set
`COIRE_RESOURCE_RESULT` to a private JSON output path. Normal execution takes a full fifteen-minute
baseline, the real cold-start/recovery guard, and a separate fifteen-minute armed idle window;
`--diagnostics` enables the diagnostic profile. The idle window begins only after Core is elected
and both Studios remain standby for fifteen seconds. Setup samples are retained separately;
startup/campaign CPU must not be presented as the steady idle measurement.
Both Studio windows include the normal native metrics collector and OTLP export. Set
`COIRE_RESOURCE_OTEL_IMAGE` to a locally loaded, policy/scanned collector digest. The runner
creates an isolated production collector with loopback-only OTLP and scrape ports; Studios reach
it through private SSH Unix-socket forwards. Completion requires actual participant metrics from
all three hosts and, with diagnostics enabled, accepted trace batches. Collector evidence is saved
beside the resource JSON, then its owned container/network are removed. Closed-port exporter
failure measurements remain separate from the normal healthy-collector idle gate. Actual host
swap comes from the kernel through psutil, without launching a subprocess inside the instrumented
worker. Supply
`COIRE_RESOURCE_FRONTEND_IMAGE` with an already-loaded, policy/scanned digest to include the real
Docker lifecycle loop. Each trial precreates and later removes its own hardened containers/internal
networks; it does not adopt an installed frontend. The collector's temporary listeners bind only
to Core loopback; no firewall rule is required.

`--faults` waits out the real missing-record and recovery guards, kills only its owned Core process,
and measures promotion and restart/hand-back against the unchanged 60/90-second bounds. With a
frontend image, readiness is checked using its scoped monitor inside the private container network;
anonymous inference must be refused. This does not prove physical power loss, public ingress,
or Core SQL/cohort recovery. Keep those acceptance rows separate. The fixture's lifecycle status
path is unavailable, deliberately exercising conservative drain accounting.

`validation/native_cluster.py` runs only on the Studios through native node-owned managers, with
admin-acquired checksum-verified assets. It covers TP/PP buffered and streaming output plus either
rank's mid-stream loss. The approved temporary RDMA address setup is an operator prerequisite;
remove it after the run. Never place commands inside question widgets.

`validation/native_frontend.py` sends buffered and streamed requests through the actual hardened
frontend container to an authenticated native node listener and its owned tiny engine on edge-a.
It uses the already-approved temporary control listeners 19440/19441 and outbound qualification
network, with a local TLS identity issuer and signed quorum fixture. Supply the approved-network
flag, checksum-verified model path, pinned image, source identity manifest and private result path.
The fixture removes its container/network, stops its engine and closes both listeners. This proves
the container-to-engine path; real election timing, physical faults and Cloudflare remain separate.

`validation/resource_sequence.py` can watch an explicitly identified same-user diagnostic runner
and start the lean trial only after that process exits. It verifies the runner's result path and
pinned images, retains separate logs and measurements, and writes a persistent sequence status.
Its measurement summary requires full baseline/armed windows and real telemetry. An adopted
process's exit code is unavailable to this supervisor: inspect the original session and cleanup
log before accepting it. The sequence never closes T073 or the release gate automatically.

`validation/armed_cohort.py` installs a fresh isolated Core cohort through the normal deployment
and protected Source qualification paths, then runs actual failed-candidate executor restoration
and a successful executor handover while native Studio peers participate. It uses temporary
control port 19460 and records observed ownership, phases and ready-origin exclusivity. On success
it removes owned containers/networks, including retired executor containers, while retaining the
private authority records outside Docker; successful disposable volumes are removed. This is executor handover evidence; full application
maintenance, Studio environment replacement and physical power loss are separate acceptance cases.


`validation/application_upgrade.py` is the live application/native matrix on Core with installed
Studio Sources and guards. It first requires both full resource profiles to qualify. Its isolated
cohort enables `upgrades,chat-files,mcp,ops`, pins ops to the existing verified registry UUID,
and directs native training lifecycle callbacks to its isolated Core port 19470. All inventory
owners must report authoritative observations; an omitted service or a callback using production
credentials must be corrected, never treated as empty work. Allow the real 301-second initial
election safety quarantine and normal terminal cleanup before another operation. The runner
covers Core upgrade/manual rollback/broken-candidate restoration, then real installed native
replacement and restoration, and records ready-origin observations and metadata fingerprints.
A failed runner preserves children, volumes and ownership journals for normal recovery; inspect
its private root before retrying. The six-case matrix passed on 2026-10-15 in private project
`coire-020-application-bb59eadcfb`, with unchanged metadata and complete owned cleanup;
see `review.md` for exact jobs, durations and retained failed attempts. Physical
power/cable faults and provider ingress remain separate acceptance gates.


For a fresh application matrix, set `COIRE_020_API_IMAGE` to the tested immutable API
image ID and `COIRE_020_NATIVE_SOURCES` to the private JSON map produced by
`validation/application_source.py` on the two Studios. The latter installs an independent
locked Source and ordinary first-install bootstrap; it never adopts an unresolved old
maintenance prefix. The private node supervisor supplies only launchd-equivalent process
restart after verified guard selection; it does not select a runtime or mutate authority.
After interruption use `validation/application_resume.py` against the original cohort's
private configuration and receipts. It refuses active operations, unreleased maintenance,
wrong immutable Source, or missing positive custody. Never reset a hold to make it resume.
Cleanup stops only positively owned fixture processes and frontends; genuinely failed
fixture engines are closed through authenticated node stop routes. Retain redacted results and necessary host journals, then remove
all stopped project containers and disposable volumes after terminal success. A failed
fixture needs explicit idle/ownership verification and archived recovery evidence before
decommission; retaining historical evidence does not require keeping containers in OrbStack. A suspended/expired cohort is historical failed evidence, not a clean baseline.

The prepared Cloudflare resource bundle and credential-file placeholders are described in
`deploy/cloudflared/README.md`. Public acceptance requires actual account entitlement,
account/zone/hostname, three tunnel IDs/credentials, monitor secrets, and inspection of
Access/cache policy. None can be inferred from a passing local template test. As inspected
on 2026-10-15, no host has a cloudflared binary; Core's Access issuer and audience are unset.
