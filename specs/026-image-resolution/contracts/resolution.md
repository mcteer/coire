# Contracts

POST /api/v1/admin/models/{model_id}/image-resolution-qualification accepts the strict
shared request and returns a durable qualification receipt. Admin-only, audited,
registry-ready image base and existing pinned local replicas; duplicate active request
is conflict. GET existing model/acquisition detail exposes durable validation status.

Existing /api/v1/images and /v1/images/generations accept integer requested dimensions
within qualified profile and bounded padded area. SSE and output authorization unchanged.
Node validation request adds optional size bounds; result adds measured-size evidence.
OpenAPI and generated TS updated in the implementation commit.
