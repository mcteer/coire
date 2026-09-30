# Image telemetry

Image admission remains disabled. The Coire Image dashboard and image alerts are
provisioned in Grafana and Prometheus so later services can start emitting into a
reviewed, content-free contract. Empty panels are expected before activation.

Inspect `coire_image_requests_total` by operation/outcome, `coire_image_node_stages_total`
by stage/outcome and `coire_image_purge_oldest_seconds`. The failure alerts fire on any
failed API operation or Studio stage; the purge alert fires after a 24-hour backlog.
Metric labels contain fixed enums only. Job IDs may appear in structured logs and spans
after ULID validation; prompts, recipes, paths, tokens and grants never belong in these
telemetry fields. To stop image work, use the image cancellation/worker runbook once those
services ship, keep `COIRE_IMAGE_ENABLED=false`, and use this dashboard to confirm idle
stages and cleanup before rollback.
