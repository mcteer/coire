# Quickstart: Validate the separated network fabrics

This guide is the acceptance procedure for feature 022. Do not disconnect core from Thunderbolt
until §§1–4 pass. Record real-cluster figures in the PR per Principle VII.

## 1. Contract and unit gates

```bash
uvx --from openapi-spec-validator openapi-spec-validator \
  specs/022-separate-control-data-fabrics/contracts/network-api.yaml
uv run pytest -q packages/coire-core apps/coire-api/tests/unit \
  apps/coire-api/tests/contract apps/coire-node/tests/unit apps/coire-node/tests/contract
uv run mypy apps/ packages/
# Generate/check OpenAPI through coire_api.app:app until an export-openapi CLI is added.
```

Expected: v1 and v2 registration fixtures both pass; a response matches its request version; invalid
core/data endpoint combinations fail; control and data clients never retry onto the other fabric.

## 2. Simulated topology

Run the integration compose override with one control network shared by all three simulated hosts and
one internal data network attached only to the two node agents.

```bash
COIRE_INTEGRATION=1 uv run pytest -q -m integration \
  tests/integration/test_network_fabrics.py
```

Expected:

- both nodes register through control DNS;
- core has no data-network attachment;
- model export succeeds node-to-node on the data network and is refused on control;
- severing data leaves health and a fake single-node engine usable;
- severing one node's control path marks only that node unreachable.

## 3. Real-cluster preflight

With the old cable topology still recoverable, run the deployment preflight from core. It must verify:

1. UniFi DNS resolves `coire-core.lab`, `coire-edge-a.lab`, and `coire-edge-b.lab` to the isolated VLAN;
2. 200 authenticated probes per Studio succeed without degraded-path events, with latency
   distribution recorded;
3. only the allowed firewall matrix succeeds;
4. the tiny model produces first token within 1.5 s p95 for ≤4k-token prompts;
5. the representative multi-tool loop adds ≤100 ms p95 control overhead per tool round trip;
6. an image result reaches core and is removed from the Studio; and
7. direct Studio transfer counters, latency, and bandwidth are recorded.

Any reliability or workload-level failure stops the cutover. Health-probe latency is diagnostic;
first-token and tool-loop criteria are the user-visible performance gates.

## 4. Software rollout

Apply the additive database migration and upgrade core services first. Confirm a legacy agent can
still register and receives a v1 response. Upgrade edge-b, verify its v2 endpoint set, then edge-a.
Confirm both nodes use control DNS and the peer replication client resolves only `.fabric` names.

Do not remove legacy columns or registration support in this feature.

## 5. Physical cutover

Drain replication and sharded jobs. Apply the generated Studio-only data hosts and firewall policy.
Remove core's Thunderbolt Bridge configuration and its cable, leaving one direct edge-a/edge-b link.
Restart node agents and verify:

- core reaches each Studio directly over the VLAN;
- edge-b remains reachable while edge-a's node agent is stopped;
- core has no data-fabric address or route;
- each Studio can serve the tiny model alone; and
- a peer model copy uses only data-link counters.

## 6. Failure injection and observability

Disconnect the Studio link. Replication and tensor-parallel admission must fail closed, while node
health and single-node serving continue. Reconnect it and verify damped recovery. Then interrupt one
Studio's Wi-Fi: its control path must alert and become unreachable without changing the peer's state.

Confirm the Cluster dashboard has two control-path panels and one data-link panel, that control-path
latency remains visible, and that deliberate control loss, data loss, and forbidden cross-fabric
access fire distinct alerts with trace/log links.

## 7. Rollback

Use the deployment rollback script to restore the prior managed host mappings, listener selection,
and cable configuration. Select legacy agent registration if an older binary is restored. Do not
downgrade the additive database migration or delete v2 observations during operational rollback.

After rollback, verify registry rows, both model-copy records, engines, job history, and audit history
match their preflight snapshots.

## Execution record — 2026-08-30

- Core control plane: migration completed and all seven long-lived services healthy; aggregate
  health returned `healthy` at `http://192.168.4.10:8180/health`.
- Repository gates: Ruff format/check and strict mypy passed; pytest reported 356 passed and 75
  environment-dependent skips. Web tests (2), lint, and production build passed with pnpm.
- Images: api, mcp, scheduler, migrate, web, OTel, and agent built for linux/arm64 and passed all
  seven `scripts/image-policy.sh` rules.
- Studio staging: identical coire-core and coire-node wheels, installer, and LaunchDaemon template
  are present on both Studios in `~/coire-stage`; wheel SHA-256 values match across nodes.
- Pending real-cluster gates: both Studios require an interactive sudo step to create `/opt/coire`,
  install System-keychain secrets, install the LaunchDaemon, and apply PF/hosts policy. A Hugging
  Face token and the selected tiny-model probe are also required. No gate is recorded as passed on
  a skip.

## Execution record — 2026-08-31

- UniFi control DNS: `coire-core.lab`, `coire-edge-a.lab`, and `coire-edge-b.lab` resolve to the
  isolated VLAN on all hosts. Bare `coire-edge-b` returned `NXDOMAIN`, so production configuration
  was standardized on the FQDNs with one-release bare-name registration compatibility.
- Cold restart: both LaunchDaemons started with FQDN control endpoints, and both nodes registered
  automatically after Wi-Fi became available. Both authenticated control listeners and both
  Studio-only data listeners bound to their intended interfaces. Core aggregate health reported
  both nodes and all control-plane services healthy.
- macOS local-network privacy: the non-root LaunchDaemons required the documented system-wide
  `192.168.4.0/24` Wi-Fi exception and a reboot. Both Studios also remained disconnected at the
  login window until an operator attached a keyboard and logged in; unattended cold-start remains
  unproven and requires Wi-Fi-at-login remediation.
- Control latency observation: two independent 200-request authenticated runs succeeded and measured
  edge-a min/mean/p50/p95/p99/max = 17.8/33.3/24.9/113.4/118.5/127.4 ms and edge-b =
  18.1/31.6/24.1/111.8/119.4/240.6 ms. The 2026-08-31 clarification removed the standalone
  health-probe ceiling; SC-002 now gates on 200/200 reliability and retains these figures as
  diagnostic evidence. Workload-level SC-003 and SC-004 remain unchanged.
- CI after the FQDN, installer, telemetry-ingress, and integration-topology corrections passed all
  image builds, lint, pin check, unit tests, engine tests, and the full integration job.
- Tiny-model acquisition and replication: the audited admin pipeline acquired
  `mlx-community/Qwen2.5-0.5B-Instruct-4bit` (289,601,064 bytes). The first replication attempt
  failed closed until macOS allowed `192.168.100.0/24` as an Ethernet local-network range for the
  system LaunchDaemons. After applying that exception and rebooting both Studios, retry 2 completed
  11/11 files and both copies verified manifest
  `a28a67a75ff6df6574ef74caaa26a0bb091a5cc3add4c5686aa68072b9430072`.
- Data-path proof: edge-a `bridge0` output bytes increased from 109,696 to 580,497,820 and edge-b
  `bridge0` input bytes increased from 594 to 290,194,656 during the retry. Both directions also
  reached the peer `.fabric` listener on port 9401. Core has no `.fabric` address or route.
- Real engine: model load created engine `d58fb86b-da86-4853-b0bc-c780ff97e70c` on edge-a and it
  reached `ready` with PID 772, measured resident footprint 873,530,400 bytes, and no state reason.
  Twenty streamed tiny-model requests over the Wi-Fi control path measured first-token
  min/mean/p50/p95/max = 492.6/555.0/519.8/663.0/957.3 ms, passing SC-003's 1.5 s p95 target.
- Remaining acceptance dependencies: representative tool-loop and image-result surfaces are not
  implemented in the current feature sequence, so SC-004 and SC-005 cannot be honestly exercised
  yet. Deliberate Wi-Fi failure injection also remains to be performed; these gates are not
  recorded as passed.
- Thunderbolt failure and recovery: with the direct Studio cable physically disconnected, both
  control nodes remained healthy, the link monitor repeatedly reported `ip_state=down` with a
  timeout, and peer port 9401 timed out in both directions. The loaded edge-a model continued to
  serve with a 524.4 ms first token. Reconnecting the cable restored `ip_state=up` without a node
  restart, port 9401 became reachable in both directions, both control nodes stayed healthy, and
  the model served again with a 782.5 ms first token.
