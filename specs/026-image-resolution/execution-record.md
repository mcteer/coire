# Execution record — 2026-10-16

The deployed image workspace uses a compact form, guided source/mask/reference inputs,
exact aspect ratios, a thumbnail-only library, overlay actions, selection and confirmed
bulk deletion, a contained full-image dialog, and permanent generation-event removal.
Reference research and rationale are in [ui-references.md](ui-references.md).

## Real Studio qualification

Existing admin-acquired Z-Image model `48075b57-b6b9-48bf-9ace-9438405ccc3d`
passed qualification `a700ed07-b42b-4e30-b7b8-b71f2153c9bc` on both existing pinned
copies. No model was acquired or loaded on Core. Both workers generated 1980×1980,
1980×1113 and 1113×1980 txt2img, plus 1980×1980 img2img. Maximum sampled physical
footprints were 66,595,809,456 bytes on B and 66,657,315,112 bytes on A, below each
112 GiB qualification reservation. Native mflux VAE tiling bounds expanded decode.
The failed earlier attempts retained the prior ready capability; the first exposed
missing node-boundary forwarding, and the second exceeded memory and was cancelled
through the authenticated node API. Added boundary tests and an owned-worker memory
watchdog, including a subprocess termination regression, cover these failures.

Published text/transform bounds are now 512–1980 per axis, 3,920,400 pixels, default
1024. Exact 16:9 at the maximum selected scale is 1968×1107, with 1107×1968 portrait.
Reference control retains its separately measured 512×512 bounds; expanded base
qualification does not qualify an auxiliary control or Fill model.

## Owner acceptance and cleanup

Four neutral teapot requests passed the real owner path: 1980×1980, 1968×1107,
1107×1968 and 512×512. Downloaded PNG dimensions, embedded recipe dimensions and
registered SHA-256 values matched. Reuse restored 1980 sizing. The first request was
submitted through the live WebKit interface, with immediate queued/progress feedback.
A separate queued request verified refusal to delete active history, then cancellation
and terminal history deletion. No prohibited image generation tests were performed.

Live WebKit then deleted the four completed history entries, verified their four
library photos remained downloadable, and permanently bulk-deleted those four photos.
Read-only SQL confirmed zero remaining events for those four jobs, four hidden-history
markers and four deleted/purged output records. Minimal job provenance and audit rows
remain; deletion does not erase security audit records. No user-owned photo was deleted.
Private evidence is under `~/.coire/qualification/026/`; generated images are excluded
from Git.

## Visual verification

Headless Playwright WebKit inspected 14 deployed populated states across desktop/mobile,
light/dark, guided modes, hover actions, selection, confirmation and preview. Keyboard
focus stays within the modal; Escape closes it and restores thumbnail focus. The later
reported clipping was reproduced with a real 1980px original: a 1166px image exceeded a
720px frame because the grid's intrinsic minimum grew its track. The dialog now uses a
viewport-bounded flex layout and zero-minimum grid track with contained image sizing.
Real square/portrait/landscape originals passed nine deployed combinations: 1440×1000,
390×844 and 844×390. Screenshots were inspected. Library metadata blocks are removed;
classification diagnostics remain available in the opened preview.

## Checks and activation

- 447 image API tests passed before the additive per-mode bounds change; 19 focused
  mode/catalog/admission tests passed afterward.
- 47 native pipeline, validation, node boundary and qualification budget tests passed.
- 297 web tests in 68 files passed after the preview and thumbnail changes; build and
  ESLint passed. Strict mypy passed across 1320 source files. Ruff checks and formatting
  passed on changed implementation paths. Generated OpenAPI freshness passed.
- Migration 0036 upgraded the live database; a temporary owned database verified populated
  downgrade/re-upgrade without losing job/event rows, then was removed.
- API, scheduler, migration, web, Prometheus and Grafana images passed image policy,
  critical-vulnerability scans and SBOM generation. Alert rules passed promtool (33 rules).
- Native nodes were upgraded through the immutable installer and launchd-owned node
  restart; existing chat engine custody was preserved. API/scheduler/web and observability
  were replaced individually, preserving existing hardened configuration and networks.
- The final API/web are healthy. Rollback compose definitions and pre-migration dump are
  private; never roll the API back to a pre-safeguard build with image generation enabled.

## Safety limitation

[027](../027-image-child-safety/spec.md) is included. Text normalization/heuristics reject
recognized sexual-minor combinations for every role, including admins, and unknown
outputs fail closed for ordinary users. Refusals were verified through both image APIs
without creating jobs. This is not comprehensive semantic moderation or reference-image
age detection; implicit requests, unsupported language and novel obfuscation remain gaps.
Do not describe this guard as a guarantee that the platform is safe for children.

Cleanup also removed the six unused task-created native candidate environments after
checking they were neither current nor process-referenced, and stopped the owned local
Vite preview. Current environments and the pre-change rollback baseline remain. The
successful qualification row is terminal with reservation_cleanup_pending=false.

Review: [PR108](https://github.com/mcteer/coire/pull/108), stacked on the existing
chat-composer branch. All026 implementation and acceptance tasks are checked.
