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

## macOS Local Network refusal

If `GET /api/v1/admin/network/links/studios` reports `ip_state: down` with
`[Errno 65] No route to host` while an SSH-launched process can reach the peer's
`.fabric` address, check System Settings → Privacy & Security → Local Network on the
affected Studio for a Coire or Python entry. The node installer signs its uv-managed
dedicated `coire-node-python` executable with the stable ad-hoc identity
`com.coire.node.runtime`, leaving uv's shared interpreter untouched. Restart `system/com.coire.node` after
an interpreter update and repeat the data-link probe. A successful SSH probe alone does
not establish that the LaunchDaemon can reach the peer.

Older installs signed the shared `python3.13` executable in place as
`com.coire.node.python`. macOS may retain both its previous identity and its signed
identity under two identical `python3.13` rows. On Studio B, either UI switch changed
only the previous identity's policy while the signed identity remained denied. Do not
use the switch appearance as proof of permission: verify the installed daemon's
authenticated `/node/data-link`. Install the dedicated runtime and grant its distinctly
named `coire-node-python` entry from the logged-in user's Local Network settings.
The copied interpreter also receives a deterministic, distinct Mach-O build UUID
before signing. Renaming and re-signing alone preserves Python's original UUID and
can make macOS associate the copy with the previous identity; Apple's
[TN3178](https://developer.apple.com/documentation/technotes/tn3178-checking-for-and-resolving-build-uuid-problems)
and TN3179 document this UUID requirement. Only the copy's `LC_UUID` changes;
uv's source interpreter and executable code remain untouched. Unsupported or malformed
Mach-O files fail installation before publishing the runtime.
The installer creates a new immutable environment, smoke-checks it, and atomically
switches `envs/current`; rollback points that link at the previous environment and
restarts the node. Existing older installs on other nodes can retain their working
environment until the next planned upgrade. Apply the same runtime preparation on
both Studios; permission grants remain per-host.

If app permission is unavailable, [Apple's TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy) documents the
`AllowedEthernetLocalNetworkAddresses` preference for a dedicated Ethernet subnet. It
affects every process on that Mac and takes effect after a Mac restart. Obtain explicit
operator approval before setting it, as required by repository network policy. Keep any
exception confined to the needed peer address (`192.168.100.12/32` on edge-a or
`192.168.100.11/32` on edge-b) and verify the authenticated
data-link probe and a real acquisition retry afterward.

## Kill and recover

Stop a node agent with `sudo launchctl bootout system/com.coire.node`. Restore it with
`sudo launchctl bootstrap system /Library/LaunchDaemons/com.coire.node.plist`. Engine processes are
left running and are re-adopted after restart.

## Roll back

Reconnect the recoverable legacy cable arrangement, then run
`deploy/cluster/scripts/rollback-fabrics.sh --apply`. This selects legacy registration/listeners on
both Studios. Restore the legacy managed hosts block documented in ADR-0002. Do not downgrade
`0003_node_endpoints` or delete v2 observations. Verify the preflight snapshots still match.
