# Runbook: control and data fabrics

Feature 022 separates the isolated VLAN control path from the direct Studio Thunderbolt data
path. Core never has a `.fabric` address and never appears in the JACCL hostfile.

## Observe

Run `deploy/cluster/scripts/preflight-fabrics.sh` on core. A full run requires executable gates in
`COIRE_TINY_MODEL_PROBE`, `COIRE_TOOL_LOOP_PROBE`, and `COIRE_IMAGE_RESULT_PROBE`; a missing gate is
a failure, not a skip. Grafana's **Coire Cluster** dashboard has one control panel per Studio and a
Studio data-link panel. Alerts distinguish control loss, data loss, forbidden path use, and latency.

## Preflight and cutover

1. Snapshot registry rows, model-copy rows, engines, jobs, and audit history.
2. Run the full preflight while the legacy cable arrangement remains recoverable.
3. Drain replication and sharded jobs.
4. Run `sudo -E deploy/cluster/scripts/apply-fabrics.sh --apply`.
5. Confirm both Studios answer `http://<node>:9400/node/health` directly over control DNS.
6. Confirm the generated JACCL hostfile contains exactly the two `.fabric` Studio names.
7. Disconnect core from Thunderbolt, leaving only the direct Studio link.
8. Reboot each node separately and repeat the authenticated health and tiny-model checks.

Never disconnect or reconfigure the cable before the full preflight stamp exists.

## Failure injection

- Disconnect Studio Thunderbolt: replication and sharded admission must fail; control health and
  single-node inference must remain available.
- Stop edge-a's node agent: edge-b must remain directly reachable from core.
- Interrupt one Studio's Wi-Fi: only its control alert may fire; the peer state must not change.
- Request an export route on port 9400: it must return 404 and increment the forbidden-path metric.

## Kill and recover

Stop a node agent with `sudo launchctl bootout system/com.coire.node`. Restore it with
`sudo launchctl bootstrap system /Library/LaunchDaemons/com.coire.node.plist`. Engine processes are
left running and are re-adopted after restart.

## Roll back

Reconnect the recoverable legacy cable arrangement, then run
`deploy/cluster/scripts/rollback-fabrics.sh --apply`. This selects legacy registration/listeners on
both Studios. Restore the legacy managed hosts block documented in ADR-0002. Do not downgrade
`0003_node_endpoints` or delete v2 observations. Verify the preflight snapshots still match.
