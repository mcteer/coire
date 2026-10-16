# Feature 025 execution evidence — 2026-10-16

Implemented the reference ivory/navy/gold palette, self-hosted Inter/Cormorant Garamond,
persistent role-aware sidebar, responsive keyboard-accessible drawer, shared workspace
components, focused chat/history/model picker/composer, sign-in and explicit persisted themes.
Suggestions only fill drafts. Overview excludes historical stopped/failed instances from active
counts. Existing authentication, API contracts, mutation guards and inference paths are retained.

## Validation
- Entire web suite: **64 files, 257 tests passed**, no skipped tests.
- Strict TypeScript/Vite production build, ESLint, Prettier and git diff whitespace checks passed.
- Ruff check/format (2393 files), strict mypy (1308 sources), OpenAPI freshness passed.
- Browser preview: 20 desktop/tablet/phone/light/dark/long-conversation scenarios;
  no page overflow and no browser exceptions. Long code fixture uses actual components
  with synthetic messages; no production conversation or inference was created.
- Actual deployed owner browser walkthrough: sign-in, Chat, Models, Instances, Users & keys,
  Runs & jobs, Images, Training, Admin, theme reload, suggestion prefill, mobile menu/Escape,
  sign-out and rejected revoked-cookie replay. 18 screenshots, no page overflow/page errors.
  Screenshot timing allows CSS transitions to settle before capture.
- Normal text token pairs measured 4.91–15.90:1; button text 5.48:1 light / 8.30:1 dark.
  This measures the documented core pairs, not an exhaustive WCAG certification.
- Hardened web image: all seven image-policy rules passed; Trivy CRITICAL findings **0**
  (including unfixed); SPDX SBOM produced. No package dependency was added. Two static
  font assets use SIL OFL1.1; full notices, sources and SHA256 hashes accompany the assets.

## Activation and rollback
Activated only `coire-web` on the existing approved https://coire-core.lab:8443 listener.
Image: `sha256:d55ed127a05ad8d597ac60859d64735b16420afa5ecb576638c38059fc891253`.
Previous web: `sha256:28866623610db50c87ad9b4aa8925ddc48c79ecd88e02a1bb39ff8eaf3ec0971`.
All other 14 Compose container identities/images were preserved. Web healthy; HTTPS root200,
HTTP browser redirect308 and original machine health200. No API/db migration or Studio action.
Training's existing disabled responses remain visible; this redesign does not enable training.

Private receipts/screenshots/SBOM and exact web-only rollback manifest are under
`~/.coire/qualification/025/`. Follow `docs/runbooks/web-interface.md` to restore only the
previous web image. The older pre-sign-in rollback file is not appropriate for this feature.
Owned loopback preview server stopped; temporary visual-fixture dist files removed.
Browser processes closed and image-policy containers removed; no persistent test volume created.

## Constitution
I/II/V: no engine, model lifecycle or placement changes. II-a: existing separate hardened web
container only. III: generated wire contracts unchanged. IV: existing authentication/scopes,
confirmation and Keychain credentials retained, no external runtime asset requests. VI:
existing route telemetry/dashboard/alerts retained; no new service/backend path. VII:
spec/plan/tasks precede implementation, complete frontend regression and live acceptance gates.
