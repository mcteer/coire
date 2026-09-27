# Quickstart: Sharded Serving over JACCL

1. Confirm both Studios are healthy on the Wi-Fi control fabric and the direct Studio-to-Studio
   Thunderbolt data fabric is connected; core must have no data-fabric address.
2. On both Studios confirm RDMA is enabled. On edge-a generate and validate both inventories:
   `deploy/cluster/distributed_config.sh --generate jaccl deploy/cluster/generated/jaccl-hostfile.json`,
   `deploy/cluster/distributed_config.sh --generate ring deploy/cluster/generated/ring-hostfile.json`,
   then run `deploy/cluster/distributed_config.sh --check` against each output. Verify both contain
   only edge-a rank 0 and edge-b rank 1.
3. Trigger the admin link probe. Record JACCL and ring results, then verify a second failure is
   required to mark down and three successes are required to restore up.
4. With a verified <=1 GB model copied to both nodes, create `sharded:tp`, await `ready`, inspect
   `/api/v1/state` for two ranks/two reservations, and stream a gateway request.
5. Drain it and verify both processes and reservations disappear. Repeat with `sharded:pp`.
6. Disconnect/disable RDMA, rerun the probe and confirm TP refuses while PP and an existing
   single-node request continue. High latency alone must not refuse TP.
7. Start a sharded stream, kill rank 1, and verify a terminal typed SSE error with retry guidance
   (or HTTP `503`/`Retry-After` if headers were not committed), whole-group teardown, node
   degradation and a bounded single-node fallback when a smaller variant fits.
8. Run the three-placement benchmark twice and verify six append-only result rows plus GPU cores.
9. Capture instance transitions, audit rows, link/rank metrics, dashboard panels and alerts.
