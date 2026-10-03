# ADR 0011: Keep image generation on bare Studio engines with fenced core publication

## Context

Feature 015 adds image assets, queued jobs, reproducible PNG recipes and private
gallery storage. Earlier architecture prose described a possible image-service
wrapper, URL-carried output access and metadata forms that differ from the approved
feature spec and the project's binding constitution. Image execution also competes
with chat for the same Studio accelerator and requires measured admission.

## Decision

The versioned `ImageSpec` and other wire models live in `coire-core`. The native API
and OpenAI-compatible adapter resolve only published registry IDs and validated
presets. Admin-only acquisition validates image assets and their licences, records
manifests and verifies both Studio copies. Coire-node owns a single resident bare
`mflux` process per Studio by default; its loopback worker credential and port never
reach a user. Core holds orchestration and blobs but loads no weights and performs
no Metal work. An inference wrapper or user-supplied node graph is outside this
design.

Each accepted job has one durable attempt and fence. The scheduler records node,
instance, memory reservation and execution lease before dispatch. Recovery observes
the node journal and cannot silently run the job again. Image dispatch shares
atomic accelerator admission with chat and requires current benchmark evidence for
the exact same-node combination. Missing, stale or failing coexistence evidence
leaves the image job queued or selects another eligible Studio.

Node output is sent to core private staging with short-lived, exact-attempt grants.
Core publishes the whole batch only after checking complete receipts, PNG recipes,
live authorization, current registry manifests, lease identity and node scratch
cleanup. A database transaction settles quota, writes all output rows, one terminal
event and completion audit. Cancellation wins by fencing publication under the same
job lock. Owner downloads require authentication and a short-lived grant carried in
a header; a URL by itself grants nothing.

Each PNG stores one bounded `coire.image` iTXt recipe. Exact regeneration means the
same pixels when inputs, model and component versions, runtime and hardware match.
An import can restore settings when those conditions differ, but it reports that
exact reproduction is unavailable. Recipe-only import checks PNG framing and at
most 64 KiB of metadata without decoding pixels; generation inputs remain capped at
10 MiB. The local CPU classifier supplies gallery tags and cannot grant access or
filter an entitled prompt.

## Consequences

Placement, classifier integration, image-kind acquisition validation and local
real-model acceptance must pass before public generation admission is enabled.
Uncertain node work retains its reservation and private staging until recovery
proves termination or cleanup. Dashboard panels, alerts and runbooks cover queue,
transfer, publication and purge outcomes.

This decision follows Constitution I (bare engines), II and II-a (Studio execution
and separate hardened services), III (shared typed contracts), IV (scoped access and
audit), V (admin acquisition and measured registry capability), VI (observability)
and VII (specification and test gates).
