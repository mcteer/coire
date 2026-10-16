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
