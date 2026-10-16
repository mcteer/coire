# Reference dimensions — issue109

Bug: Follow a reference rejected any input whose normalized dimensions did not equal
output dimensions. This was enforced at admission, scheduler manifest construction,
shared node-command validation and native Canny preprocessing. Fixes
https://github.com/mcteer/coire/issues/109 under the existing guided-reference requirement.

Plan: retain original source dimensions/digest and all ownership/readiness/byte checks;
relax output-size equality only for control references. Fit proportionally with centered
white padding onto the native output canvas before edge extraction. Preserve existing
same-size pixels, thresholds, output bounds and strict init/mask contracts. Version the
edge-cache identity. Clarify the interface's composition-only semantics. No dependencies,
new wire fields, model acquisition, migration or service/network changes.

Constitution: I/II direct native mflux and PIL preprocessing only on Studios; III shared
node command first and boundary regressions; IV owner/digest/retention checks preserved;
V existing verified base/control assets; VI existing control stage spans/metrics/logs and
image dashboard/alerts remain applicable; VII failing regressions first, followed by real
Studio owner-path generation and headless WebKit inspection.

Tests first reproduced three failures (landscape/portrait native inputs and admission).
The corrected suite covers ownership refusal, actual source manifest size, digest/size
substitution refusal, padding geometry, unchanged same-size behavior, exact thresholds
and edge cache reuse. Existing image authorization contracts are included.

## Acceptance — 2026-10-16

57 boundary/unit/authorization tests passed (integration-only test deliberately excluded
from this no-engine suite), as did 297 web tests, strict mypy across 1320 files, Ruff,
ESLint, production builds and OpenAPI freshness. No wire shape changed. API/scheduler/web
passed hardened image policy, critical scans and SBOM generation, and are healthy after
scoped activation. Both immutable Studio node environments were installed and the exact
launchd-owned node processes restarted, preserving other engines. Prior environments and
exact compose pins are retained privately for rollback.

Real headless WebKit uploaded an owned 1200×800 geometric reference, confirmed readiness,
and submitted a 512×512 vase request with the existing qualified control model. Job
`01M53AERRZ27GKX80DD2BRMGCY` completed successfully with four native generation steps.
Desktop/mobile input screenshots were inspected. Private evidence is under
`~/.coire/qualification/109/`; no user reference or output was accessed.

The downloaded output decoded to 512×512, its registered SHA-256 matched, and embedded
recipe input manifests still described the 1200×800 source. Real output dialogs passed
WebKit desktop, mobile and short-viewport checks and screenshots were inspected. The
owned output and history were deleted after acceptance; SQL confirmed output purge and
zero remaining events. The owned source was also deleted (asynchronous input purge),
and both installer-created temporary rendered plists were removed. Prior production
node builds are deliberately retained as rollback targets, not test candidates.

Review: https://github.com/mcteer/coire/pull/110, stacked on the image-workspace branch.
