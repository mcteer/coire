# Sharded serving (beta)

Sharded TP/PP is beta because it depends on macOS RDMA and JACCL. Single-node serving remains the
recovery path. Core stays on Wi-Fi and is never a rank; only the direct Studio-to-Studio
Thunderbolt connection carries generated-hostfile traffic.

## Prepare and verify

On edge-a, generate complete inventories with the versioned environment. Do not hand-author RDMA
device fields:

```bash
deploy/cluster/distributed_config.sh --generate jaccl /opt/coire/state/jaccl-hostfile.json
deploy/cluster/distributed_config.sh --generate ring /opt/coire/state/ring-hostfile.json
deploy/cluster/distributed_config.sh --check /opt/coire/state/jaccl-hostfile.json
```

Install identical read-only copies on both Studios and in core's configured cluster directory.
Trigger `POST /api/v1/admin/links/studios/probe` with the admin bearer. Three consecutive current
JACCL successes open TP admission; two failures close it. High latency is displayed and alerted but
does not close admission. PP requires a current successful ring probe.

### Bridge-preserving generation and IPv4 GID prerequisite (2026-10-05)

The generator now supports a read-only bridge-preserving mode on Studio A:

```bash
/opt/coire/envs/current/bin/python3 ~/coire-stage/016-bridge-hostfile.py --generate jaccl --generate-on-bridge --output ~/coire-stage/016-jaccl-hostfile.json
```

This invokes pinned native MLX discovery/generation and validates the existing bridge membership,
addresses and peer routes instead of moving any endpoint. The staged filename refers to the current
`deploy/cluster/scripts/studio-rdma-fabric.py` source. Generation succeeded on the restored network;
it does not establish a passing collective. The pinned `mlx.launch` console entry point must be
invoked through `mlx._distributed_utils.launch:main`; `python -m mlx.launch` is not a module in
MLX 0.32.2. The native launcher requests PTYs. Studio A's exact self-key received the separately
approved `pty` exception to its existing `restrict,from=...` scope; forwarding restrictions remain.

An actual authenticated isolated-node JACCL probe reached the RDMA runtime and failed with
**"No IPv4-mapped GID for this device"**. The bridge's IP does not give its member's RDMA device
an IPv4 GID. Do not remove/rebuild the bridge again to work around this.

The user approved preparation of `deploy/cluster/scripts/studio-rdma-alias.py`. Its alias-only
trial generates one RFC 3927 /30, adds only each node's link-local alias to the native-discovered
interface, and verifies both the mapped GID and unchanged bridge/membership/control/data routes
in five samples over ten seconds. It never changes bridge membership, existing addresses, routes
or firewall configuration. On failure it removes only its own generated alias. A private immutable
baseline remains under `/opt/coire/state/rdma-aliases/<node>.json`; failed rollback is a failure,
not proof of restoration. Operator application and independently checked alias/GID preservation
now pass on both Studios. Three authenticated isolated-node native JACCL trials succeeded at
about 9.26 GB/s and 0.16–0.17 ms. Production hostfile/link-record deployment and persistent/reboot
acceptance remain unverified.

Current staged check succeeds on both Studios with `--check --interface en5`; the generated aliases
are reported by that command. Applying the prepared trial requires an operator's root session:

```bash
sudo /opt/coire/envs/current/bin/python3 /Users/mcteer/coire-stage/studio-rdma-alias.py --apply --interface en5
```

Run independently on each Studio through its control connection/Terminal. Rollback uses the same
staged helper with `--rollback --interface en5`. Both `sudo -n true` checks currently refuse with
`a password is required`. Do not change sudo policy or transmit passwords. After operator application,
recheck authenticated peer connectivity, actual GIDs and JACCL collectives before deploying generated
hostfiles or enabling training/serving admission. These unit tests/check plans are not live alias or
collective acceptance. The historical cutover below remains suspended.

### Preserve the existing replication endpoints during RDMA setup

**Suspended after real-host failures (2026-10-04).** The direct-interface trial's apply and
route-repair CLI actions are disabled. Its route repair failed real peer verification and its
raw bridge-member rollback failed on Studio A. The commands below describe the historical
trial, not a currently approved successful procedure. Recover the existing saved bridge and
verify authenticated data connectivity before attempting further RDMA changes. Unit/script
tests are not proof of macOS interface behavior.
Both Studios were subsequently recovered by separately approved full restarts and startup unlocks.
Saved original bridge/address/member configuration was rebuilt at boot; authenticated control and
data-link checks passed on both, with successful peer SSH and zero-loss ping. This verifies the
original IP data network, not RDMA collectives. Do not repeat the retired live-interface cutover.

For the current two-Studio bridge installation, use
`deploy/cluster/scripts/studio-rdma-fabric.py` rather than upstream `--auto-setup`.
MLX's automatic IP setup would replace the declared replication subnet with a generated /30.
The helper moves each existing `.fabric` IPv4 address and netmask from `bridge0` to the
native-discovered direct interface, retaining the declared names and peer subnet. It touches
neither Wi-Fi/control interfaces nor firewall rules. This is a **runtime validation trial**;
it does not yet install persistent macOS network-service configuration.

1. Drain replication, link probes and sharded work through authenticated admin paths before
   cutover. Keep single-node/control reachability available and use `.lab` SSH for application.
2. Discover the connected interfaces with pinned native MLX `extract_connectivity` and
   `IPConfigurator`; do not infer them from model/hardware names. On 2026-10-04 both peers were
   discovered as `en5`. Recheck after a cable/port change.
3. Stage the repository helper on each Studio. With `INTERFACE` set to its newly discovered
   interface, run on that Studio:

   ```bash
   /opt/coire/envs/current/bin/python3 ~/coire-stage/studio-rdma-fabric.py --check --interface "$INTERFACE"
   sudo /opt/coire/envs/current/bin/python3 ~/coire-stage/studio-rdma-fabric.py --apply --interface "$INTERFACE"
   ```

   `--check` is read-only. Apply saves an immutable, fsynced private baseline in
   `/opt/coire/state/rdma-fabric/<node>.json` before mutation. Command/verification failures
   automatically restore the original bridge; retained baselines also permit recovery after
   process termination. Existing baselines refuse a new trial rather than being overwritten.
   If noninteractive sudo is unavailable, an operator must authenticate it in their terminal.
   Approval to perform setup does not provide that credential; do not change sudo policy.
   Apply also installs two exact host routes: the existing local `.fabric` address through
   `lo0` for native self-SSH, and the declared peer through the discovered data interface.
   It never changes the default route or adds a broader network route. Both routes must
   remain direct/static through five checks over eight seconds. This is necessary because
   a transient connected-subnet route can disappear after an `ifconfig`-only cutover.
   The Ethernet peer route uses the node's own interface **address** as its direct-route
   gateway and the discovered interface as `-ifp`. Using `-interface en5` alone is the
   point-to-point form and was observed to create a permanent self-MAC binding on these
   Ethernet-style Thunderbolt interfaces. Correct route flags/interface names alone are
   insufficient: the helper also primes ARP and requires a resolved peer neighbor on the
   data interface whose MAC differs from the local NIC. Authenticated peer TCP and collective
   verification still happens separately after both participants finish.

   If the earlier helper already moved the address but the peer/local host routes are missing,
   use the updated helper with the **original retained baseline**, on each Studio:

   ```bash
   sudo /opt/coire/envs/current/bin/python3 ~/coire-stage/studio-rdma-fabric.py --repair-route
   ```

   This completes the host routes without replaying the address cutover or overwriting the
   rollback baseline. It also replaces the earlier helper's observed self-MAC peer binding
   when the baseline records no pre-existing static peer route. Other mismatched existing
   host routes are refused. Failed installation or
   delayed verification restores the original bridge. Do not rerun `--apply` over the old
   baseline or declare success solely from an address present in `ifconfig`.
4. Once **both** Studios have completed cutover, generate native hostfiles on Studio A:

   ```bash
   /opt/coire/envs/current/bin/python3 ~/coire-stage/studio-rdma-fabric.py --generate jaccl --output /opt/coire/state/jaccl-hostfile.json
   /opt/coire/envs/current/bin/python3 ~/coire-stage/studio-rdma-fabric.py --generate ring --output /opt/coire/state/ring-hostfile.json
   ```

   Generation uses unchanged pinned MLX connectivity/device discovery and native hostfile
   generators with an explicit setup object that verifies the already-configured direct
   fabric. No MLX function or global is patched. RDMA fields come from native generation;
   data addresses are pinned to the declared `.fabric` endpoints. A bridge still up, a wrong
   interface/route, missing direct link, or existing output file refuses generation. Preserve
   old hostfiles before selecting new output names.
5. Install identical read-only hostfiles on both Studios and in core's deployment-managed
   cluster input directory. Run authenticated JACCL/ring link probes and node data-link checks;
   verify real peer transfer/digests before considering the trial successful. SSH or
   `PORT_ACTIVE` alone is not that evidence.

To restore the runtime baseline, drain distributed/replication work and run on **each** Studio:

```bash
sudo /opt/coire/envs/current/bin/python3 ~/coire-stage/studio-rdma-fabric.py --rollback
```

Rollback removes the helper's exact host routes (preserving static routes recorded in the
baseline), removes the moved address from the direct interface, restores its bridge membership,
reinstates the original address/netmask and raises the bridge, then verifies that state.
The helper now checks the unchanged persistent bridge configuration first and requests OS-managed
service reactivation instead of retrying raw `ifconfig ... addm`. On Studio A, service reactivation
returned the service to Enabled and raised the bridge but did **not** restore its missing member
or address; this recovery path is also not yet verified. Do not treat its invocation as successful
recovery. Retain original baselines and report actual command stderr and interface/route results.
Retain snapshots until peer reachability and authenticated data-link checks pass. Disable
sharded admission and restore prior hostfiles through the deployment workflow before rollback.
Do not claim reboot persistence from this trial: restart can restore macOS's prior network
service configuration. Durable service configuration and reboot validation remain release gates.

## See it

- `GET /api/v1/state` shows both ranks, both reservations, and the projected/raw link evidence.
- `GET /api/v1/admin/links/studios` shows damping counts, measurements, and flapping.
- `GET /api/v1/admin/benchmarks` preserves every single-A/TP/PP comparison.
- In Grafana, open **Coire Cluster** and inspect sharding eligibility, measurement age, group
  transitions, and benchmark throughput.
- Correlate structured `instance_id`, `group_id`, `node`, and `model_id` fields with
  `coire.scheduler.sharding.*` and `coire.api.sharding.*` spans.

## Kill or drain

Use `DELETE /api/v1/instances/{instance_id}`. The instance enters draining, waits for leases up to
the configured deadline, stops both node expectations, confirms both, and releases both
reservations in one transaction. For an incident, the same endpoint is safer than killing a rank.
If a rank is already gone, reconciliation marks the group failed, degrades that node, stops the
survivor, and makes one bounded smaller-variant fallback attempt.

Never mark a reservation free manually while either stop is unconfirmed. A retained failed
reservation is intentional evidence that memory may still be resident.

## Roll back

Drain all sharded instances, set affected models to `single:auto` or an explicit single Studio,
and deploy the previous application tag. Database migration `0008` is additive; keep it during an
application rollback so link observations and benchmark history remain available. Disable RDMA or
disconnect the Studio cable only after all groups report stopped. Single-node inference continues
over the Wi-Fi control fabric.
