# Image workspace and measured resolution

Z-Image text generation and image transformation support exact integer dimensions up to
1980×1980 (3,920,400 pixels). Choose an aspect ratio and resolution; the form displays the
actual exact dimensions. At the largest scale, 16:9 is 1968×1107 and 9:16 is 1107×1968.
Custom sizes remain bounded by the selected model/mode. Reference control currently has
its own 512×512 qualification; other models retain their own limits.

The worker pads internally to a 16px native canvas and crops to the requested dimensions
before writing recipe metadata. Large Z-Image decode uses pinned mflux's native VAE
tiling. Core never loads models. To expand another model, use the audited admin model
qualification control; existing registry-verified local copies on both Studios must pass
size evidence and physical-memory bounds before capability changes publish. Failure
preserves the previous ready profile. The worker watchdog terminates only its owned
qualification child on a budget violation or unavailable footprint. See image observability
panels and the resolution-qualification alert for failures; use authenticated job cancellation
for an owned active qualification. Do not kill unrelated engine processes.

The library shows thumbnail tiles with overlay actions and selection. Open a tile to inspect
its entire image inside a viewport-bounded dialog. Close or Escape returns focus to the tile.
Select multiple photos and confirm Delete selected for permanent output deletion. Failed
items remain selected for retry. History deletion is separate: select completed entries and
confirm; event rows are removed and history becomes unreadable, while library images remain.
Security audit and minimal foreign-key provenance are retained. Active jobs must finish or
be cancelled before their history can be deleted.

Deployment rollback uses the retained exact service compose pins and immutable node
baseline. Restore only affected services, without removing unrelated containers. Migration
0036 can downgrade by removing the nullable history marker; this makes hidden minimal
provenance visible again but cannot recover physically deleted event payloads. Keep the
pre-migration backup if an operational database restore is required. Avoid rolling back
[child-safety guards](image-child-safety.md) while generation is enabled.

Measured validation, release gates and private evidence locations are recorded in
[026 execution record](../../specs/026-image-resolution/execution-record.md).

## Reference input dimensions (issue109)

Control references keep their own normalized dimensions and digest in owner storage,
resolved recipes and node transfer manifests. They do not need to match the output.
The Studio fits the whole reference proportionally onto the requested native canvas
with centered white padding before Canny extraction (Lanczos resampling). It does not
stretch the reference or crop away its edges. The derived edge cache includes the output
size, thresholds, source digest and preprocessing version. Existing output capability,
file-size, pixel-count, authorization and quota limits remain enforced. Init/mask inputs
for other workflows retain their existing alignment contract.

The control mode follows outlines and composition. It is not an identity-preserving
portrait editor. A task such as placing the same person in a new setting requires a
reference-aware editing model and a separate qualified integration. Merely increasing
Canny strength or adding identity instructions does not supply that capability.

Roll back issue109 using the retained exact API/scheduler/web compose pins and prior
immutable native environments. The API, scheduler and node must agree on the source
manifest contract; do not deploy only an admission change to old workers. No schema or
migration is needed: existing width/height fields now correctly describe source bytes.
