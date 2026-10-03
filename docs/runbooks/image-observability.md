# Image telemetry

Image admission remains disabled. The Coire Image dashboard and image alerts are
provisioned in Grafana and Prometheus so later services can start emitting into a
reviewed, content-free contract. Empty panels are expected before activation.

Inspect `coire_image_requests_total` by operation/outcome, `coire_image_node_stages_total`
by stage/outcome and `coire_image_purge_oldest_seconds`. The failure alerts fire on any
failed API operation or Studio stage; the purge alert fires after a 24-hour backlog.
The Images dashboard also has job outcome, dispatch/queue reconciliation, worker
cancel, storage refusal and bounded cache panels. Investigate
`CoireImageDispatchUncertain` against the exact node journal before retrying;
`CoireImageWorkerStopFailures` requires process-death evidence before any memory
hold is released. `CoireImageChatRegression` watches the existing gateway
first-token p95 when image dispatches start. It is a cluster-wide warning;
the T069 same-node benchmark and profile gate still determine whether a
specific chat/image pair may coexist. An empty panel is not passing latency
evidence and no coexistence profile should be approved from it.
The `job_events` operation covers the owner-only image SSE stream. A client can resume with
`Last-Event-ID: <job ULID>:<sequence>`; an expired history returns a typed `reset` event
containing the current job snapshot. The stream rechecks live access on each poll and ends
after revocation or a terminal event. A 400 response means the cursor is malformed or belongs
to another job. The dashboard's private API panel and `CoireImageFailures` alert include
this operation without adding user content to metric labels.
The web observer uses the shared SSE transport, ignores duplicate sequence numbers, refuses
gaps, and stops reconnecting on a terminal event or 401/403/404 response. A gap is a
recovery signal; inspect the job snapshot and event history rather than resubmitting work.
The owner job list uses an opaque cursor bound to the current owner and skips jobs whose
current entitlement no longer permits a read. Empty pages can still have a next cursor
when the bounded scan contained only inaccessible jobs. A completed job read includes
published output projections, excluding classifier-explicit output metadata when live
explicit access is absent.
The private `/api/v1/images/models` picker returns ready published mflux image bases after
checking hidden dependencies and current entitlements. It advertises txt2img with zero
guidance, no LoRA, and no negative prompt until advanced worker modes ship. A base with an
incompatible default is omitted. The list is empty while image admission is disabled.
If a model is absent, inspect its published/ready state, manifest, dependency health,
and the caller's live explicit entitlement and personal-key scope.
Metric labels contain fixed enums only. Job IDs may appear in structured logs and spans
after ULID validation; prompts, recipes, paths, tokens and grants never belong in these
telemetry fields. To stop image work, use the image cancellation/worker runbook once those
services ship, keep `COIRE_IMAGE_ENABLED=false`, and use this dashboard to confirm idle
stages and cleanup before rollback.
