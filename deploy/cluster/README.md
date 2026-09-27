# Cluster network deployment

`nodes.yaml` declares identities. Control names come from UniFi DNS. `hosts` maps only the two
static `.fabric` Studio endpoints. `firewall.yaml` documents the minimum-peer policy and
`scripts/apply-firewall.sh` renders its host-specific PF anchor.

Run `scripts/preflight-fabrics.sh` before `scripts/apply-fabrics.sh --apply`. Generate the complete
two-rank JACCL inventory on a Studio with `distributed_config.sh --generate jaccl <output>`; this
uses MLX's device discovery and must not be replaced with a hand-authored template. Set
`COIRE_JACCL_HOSTFILE` to that output for preflight. Rollback changes listener selection but deliberately
leaves the additive database migration intact. See `docs/runbooks/network-fabrics.md`.
