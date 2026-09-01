# Quickstart: Instances and Cluster State

1. Apply migration `0007`, start integration compose, and declare both simulated nodes.
2. Validate a tiny model; `POST /api/v1/instances` and retain its UUID.
3. Subscribe to its events and verify ordered requested-through-ready transitions.
4. Restart the scheduler during a second launch; verify one reservation and one engine.
5. Create the variant on the other node; verify least-in-flight routing.
6. Drain one instance; verify traffic moves and stop is bounded.
7. Compare `/api/v1/state` with both node health APIs.
8. Try unknown, wrong, consumed and revoked registration tokens; verify audit rows.
9. Kill a ready tiny engine; verify failed state, prompt request errors and released memory.
10. Run all gates and repeat on the real cluster with the <=1 GB model, recording no secrets.
