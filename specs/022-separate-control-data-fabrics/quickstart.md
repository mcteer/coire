# Quickstart: Validate the separated network fabrics

This guide is the acceptance procedure for feature 022. Do not disconnect core from Thunderbolt
until §§1–4 pass. Record real-cluster figures in the PR per Principle VII.

## 1. Contract and unit gates

```bash
uvx --from openapi-spec-validator openapi-spec-validator \
  specs/022-separate-control-data-fabrics/contracts/network-api.yaml
uv run pytest -q packages/coire-core apps/coire-api/tests/unit \
  apps/coire-api/tests/contract apps/coire-node/tests/unit apps/coire-node/tests/contract
uv run mypy
uv run coire-api export-openapi --check
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

1. UniFi DNS resolves all three control names to the isolated VLAN;
2. 200 authenticated probes per Studio succeed with p95 ≤ 50 ms;
3. only the allowed firewall matrix succeeds;
4. the tiny model produces first token within 1.5 s p95 for ≤4k-token prompts;
5. the representative multi-tool loop adds ≤100 ms p95 control overhead per tool round trip;
6. an image result reaches core and is removed from the Studio; and
7. direct Studio transfer counters, latency, and bandwidth are recorded.

Any failure stops the cutover. A Wi-Fi performance failure calls for wired control networking, not a
waiver of the target.

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

Confirm the Cluster dashboard has two control-path panels and one data-link panel, and that deliberate
control loss, data loss, forbidden cross-fabric access, and latency threshold breaches fire distinct
alerts with trace/log links.

## 7. Rollback

Use the deployment rollback script to restore the prior managed host mappings, listener selection,
and cable configuration. Select legacy agent registration if an older binary is restored. Do not
downgrade the additive database migration or delete v2 observations during operational rollback.

After rollback, verify registry rows, both model-copy records, engines, job history, and audit history
match their preflight snapshots.
