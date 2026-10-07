# Cluster network deployment

`nodes.yaml` declares identities. Control names come from UniFi DNS. `hosts` maps only the two
static `.fabric` Studio endpoints. `firewall.yaml` documents the minimum-peer policy and
`scripts/apply-firewall.sh` renders its host-specific PF anchor.

Run `scripts/preflight-fabrics.sh` before `scripts/apply-fabrics.sh --apply`. Generate the complete
two-rank JACCL inventory on a Studio with `distributed_config.sh --generate jaccl <output>`; this
uses MLX's device discovery and must not be replaced with a hand-authored template. Set
`COIRE_JACCL_HOSTFILE` to that output for preflight. Rollback changes listener selection but deliberately
leaves the additive database migration intact. See `docs/runbooks/network-fabrics.md`.

For the existing bridge-based Studio replication subnet, use the endpoint-preserving runtime
trial in `scripts/studio-rdma-fabric.py`. Its check/apply/rollback and native hostfile generation
procedure is documented in `docs/runbooks/sharded-serving.md`. Application requires operator
sudo on each Studio. Do not run upstream `--auto-setup` against the current replication subnet:
it substitutes generated /30 addresses. The trial is not a reboot-persistent network deployment.

**Trial suspended (2026-10-04):** real peer verification and rollback failed. The helper now
refuses apply/route-repair actions. Follow the incident status in the sharded-serving runbook
and feature-016 execution record; the historical procedure above is not a verified working setup.

Current bridge-preserving hostfile generation uses `studio-rdma-fabric.py --generate jaccl
--generate-on-bridge --output <new-file>`. Native generation succeeded without network mutation;
the actual collective then exposed a missing IPv4-mapped GID on the native RDMA member interface.
`scripts/studio-rdma-alias.py` is the prepared alias-only trial/rollback helper. Read-only checks
pass on both Studios, but root application and collective/persistence acceptance remain outstanding.
Follow the current sharded-serving runbook; do not rerun the suspended bridge-removal trial.
