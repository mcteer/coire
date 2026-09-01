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
