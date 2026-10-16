# Implementation Plan: Professional Coire interface

**Branch**: `feat/025-professional-interface` | **Date**: 2026-10-16 | **Spec**: [spec.md](spec.md)

## Summary
Rebuild the shared shell and visual system around warm ivory, deep navy, muted gold, Inter body and Cormorant Garamond headings. Use a persistent desktop sidebar and compact top header; responsive mobile drawer; explicit light/dark preference. Coordinate chat history, compact model selection, empty state, readable messages and composer; reskin all admin, image and training components through shared tokens.

## Technical Context
Existing React18, TypeScript strict, Vite, CSS custom properties and generated API types. Reuse current hooks/routes and SVG icons built as code; no new package dependency. Self-host two Latin WOFF2 font files under SIL OFL1.1 with notices/source hashes. Browser theme storage is non-sensitive and guarded against storage failures. No API or database changes.

## Constitution Check
I/II/V: no engines, model ownership, placement or registry behavior changes. II-a: only existing hardened web image changes. III: generated contracts unchanged. IV: auth, scopes, confirmations, drafts and secrets unchanged; no new network/CDN calls at runtime. VI: existing route telemetry retained; no new backend path or misleading fabricated status. VII: spec/plan/tasks before code; existing interaction tests and meaningful navigation/theme/suggestion regressions, visual browser acceptance and image policy/scan required. No exceptions.

## Project Structure
`components/AppShell.tsx`, `components/Icons.tsx`, `hooks/useTheme.ts`, existing chat components and pages/SignIn; `styles/tokens.css`, `app.css`, `chat.css` plus coherent cross-page rules. `public/fonts` stores font assets and licences. Update `docs/design` canonical design and operational UI runbook. No external icon library or decorative image generation required.

## Validation and rollout
Run entire web suite, strict TS production build, ESLint and Prettier; API schema freshness unchanged. Run meaningful browser keyboard/mobile/light/dark/navigation/streaming/long-content checks with real authenticated local read paths; representative long chat uses isolated route fixtures without creating production conversations. Build/policy/critical scan/SBOM web image, stage rollback, recreate only web on already approved TLS, verify actual owner login and all pages. No full backend test rerun required for CSS/React presentation-only changes. Remove owned test containers/volumes/processes.

## Revision plan
Reinspect the supplied reference in WebKit. Replace heavy dark light-theme navigation and
white cards with exact paper100/200/400 surfaces and ink tokens; increase undersized
control/label typography, reduce marketing-scale chat headline and align page spacing.
Use consistent outlined secondary actions and bounded readable content widths. Verify
screenshots in headless WebKit with actual auth/data plus isolated long-content fixtures.
Correct the explicit entitlement UI bug using existing admin/API contracts, add model-form
content mode with inherited preset policy and restored recipe safeguards, then test those
boundaries and perform an adult nonsexual anatomy generation through coire-node.
Rebuild/scan/redeploy web only with a fresh rollback snapshot; no backend or node changes
expected. Record any actual generation prerequisite precisely rather than claim support
from a selector alone.

Live negative acceptance exposed issue102: late admission ImageForbidden was correctly
refused but lacked an audit row. Amend scope to roll back admission locks before invoking
the existing independent content-free refusal audit helper. Add a route contract regression,
run relevant backend image tests/type/format/schema gates, build/policy/scan/SBOM and replace
only the API image after web acceptance. No schema, migration, permission or listener change.

## Literal reference correction
Remove the desktop sidebar and branded hero treatments. Reuse existing nav/guards in a
64px horizontal masthead; retain mobile keyboard drawer. Place Chat and sign-in in the
measured672px column, masthead brand at144px on1440px viewport, main heading32px/40px.
Admin subnavigation becomes a horizontal text rail; operational data uses open ruled
sections and wider breakouts only where tables/forms need space. Copy measured CSS values
for both themes, including brown/gold buttons rather than invented inverse gold buttons.
A dedicated reference stylesheet applies the canonical theme after existing feature layout
styles; preserve generated contracts and all explicit image controls/permissions. Add
meaningful system-theme and accessible-history behavior tests where behavior changes.
Produce real headless WebKit comparisons and a measured style parity receipt; deploy web
only after build/tests/lint/image checks, preserving the installed API audit fix.
