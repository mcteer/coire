# Implementation Plan: Image resolution and aspect ratios

Branch: `feat/026-image-resolution`. Spec: [spec.md](spec.md).

## Summary / Technical Context
Existing Python/Pydantic shared contracts, native mflux Studio worker, SQLAlchemy
registry acquisition reconciler and React/TypeScript image form. No dependencies.
Relax requested dimension alignment to integer pixels; internally derive a 16-aligned
canvas, run native mflux on that canvas, verify returned size and center-crop to the
exact requested dimensions before existing metadata/classifier/upscale/output stages.
Bound padded pixels as well as output pixels. Aligned existing requests are unchanged.

Introduce typed admin qualification and optional node validation bounds. Reuse the
durable DownloadJobRow validation cursor at VERIFY_REPLICA on existing local copies,
without pull/export/import. Preserve the ready model and old capability while both
copies validate; publish expanded bounds only after both pass. Store prior/profile,
requested bounds, reservation size and cleanup state in the existing validation JSON.
Audited failure preserves prior usable capability. Retry reservation cleanup durably
for terminal qualification jobs. Sampling covers max square and both non-square
orientations, txt2img and img2img, including native alignment crop. Update model memory
estimate conservatively from measured delta; do not exceed live ledger capacity.

Image form adds size and exact aspect-ratio choices constrained by measured capability,
plus existing custom width/height and a small aspect preview. Align ratio in whole
output pixels (not latent pixels). Restore/custom imports are preserved. Requalifying
existing copies is an authenticated audited admin action and visible in model detail.

## Constitution Check
I: bare mflux unchanged; only node spawns validation/engine work. II: no Core inference;
II-a: existing hardened services only. III: shared models first, generated OpenAPI/TS
and contract tests. IV: live scoped human-admin qualification and audit; existing owner
image authorization. V: registered, acquired, verified local copies only, no HF pull.
VI: existing validation spans/metrics and failed-validation dashboard/alerts expanded
with qualification outcome signal. VII: spec/plan/tasks before code; contract/unit,
real Studio both-copy and WebKit gates. No exception or migration required: durable
cursor uses existing typed JSON evidence storage.

## Structure and Delivery
Shared images/image_worker schemas; node image_validation and runtime pipeline/control;
API image qualification service, admin route and registry reconciler; web images API,
ImageForm and admin model qualification control. Unit/contract tests first per boundary.
Build/test native node package; install immutable tested candidate via existing native
ops procedure with exact rollback. Build/scan/SBOM API and web independently. Qualify
existing published Z-Image copy via audited admin path. Accept 1980 square and portrait/
landscape downloads/metadata plus cancellation and old 512 recipe regression. WebKit
inspection at 390px/desktop in both themes; cleanup artifacts and workloads, record
measured results. UI must not advertise expanded sizes before qualification passes.

## Image workspace expansion
Use a compact top workbench above the gallery, guided mode-specific source inputs,
and advanced settings disclosure. Gallery cards prioritize images with sibling overlay
icon buttons (never nested interactive controls). A large accessible dialog loads private
image blobs and revokes object URLs on close. A selection toolbar batches existing
owner-authorized image deletion and retains failed selections with actionable errors.

History deletion needs shared request/receipt schemas, authenticated owner routes,
contract tests and a reversible migration for a history-deleted marker. Delete event
rows permanently while retaining minimal job provenance required by library outputs;
filter hidden history consistently from list/detail/event access. Preserve audit rows.
This supersedes the earlier statement that no migration is required: resolution itself
uses existing JSON storage, while history deletion introduces persistence changes.

Reference inspection and the resulting design choices are recorded in ui-references.md.
No claim of completion is made until actual deployed WebKit inspection and tests pass.

Qualification execution correction: persist/forward bounds across node route, durable params
and worker reconstruction. Use pinned native mflux TilingConfig for expanded-size VAE decode,
retain original512decode, and enforce physical memory during qualification with a worker-owned
watchdog that persists failure and exits even if native evaluation has not returned. Both-copy
evidence remains mandatory; failed/aborted runs preserve old limits and clean reservations.
