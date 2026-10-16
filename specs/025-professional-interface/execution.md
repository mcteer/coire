# Feature025 — restored sidebar and color themes,2026-10-16

The owner rejected the literal-reference layout, requested the preceding sidebar presentation,
and explicitly chose **both presets and custom themes**. The current implementation supersedes
the literal layout. Earlier execution records below are historical evidence only.

## Restoration and current behavior
- Immediately restored validated web135c7a20 to the existing TLS listener, then restored the
  matching1f026b60 presentation in source. Removed the literal reference stylesheet.
- Kept the installed API audit fix and existing explicit image controls/entitlement unchanged.
- Added header/sign-in **Color themes** dialog: Original, Ocean, Forest and Plum presets;
  up to12 named custom themes with separate light/dark page, panel, sidebar and accent colors.
- Live preview, save, edit, cancel/Escape, delete and Reset colors. Reset retains saved customs.
  Original clears only palette-owned CSS variables and restores the baseline styling exactly.
- Only colors vary; typography, navigation, spacing, data and image settings stay fixed.
- Versioned bounded browser-local JSON preference at `coire.color-themes.v1`; existing
  `coire.theme` still handles appearance. Private drafts/credentials are never stored here.
- Strict six-digit colors/names/IDs, bounded collection, malformed/stale-data fallback,
  storage-blocked message without blocking navigation, no arbitrary CSS or remote assets.
- Derived text/accent/button/sidebar/semantic label colors target4.5:1 against their surfaces;
  incompatible page/panel combinations cannot save. Focus colors remain readable for accents
  that blend into the background. No claim of exhaustive accessibility certification.

## Validation
- **283 web tests,66 files passed**, no skips. Added persistence/private-draft isolation,
  custom lifecycle/cancel, malformed/oversized preferences, capacity/duplicate names, blocked
  storage, incompatible colors, text contrast (all preset modes and difficult gray/white
  cases), Original cleanup and keyboard-focus regressions.
- Strict TypeScript/Vite build, ESLint, Prettier and whitespace checks passed; no dependency,
  API contract, schema, database, backend or Studio change in this slice.
- Actual headless Playwright **WebKit26/build2215**:27 scenarios in preview and deployed
  interface, with screenshots inspected. No page overflow or page errors.
- Preset choice persisted from sign-in to chat; custom light/dark colors survived reload;
  edit/cancel restored saved values; deletion/reset restored Original; screenshot/geometry
  checks confirmed the sidebar did not move across palettes.
- Actual dialog Tab cycling, Escape/focus return and mobile editor scrolling were checked.
  WebKit exposed a focus escape; explicit initial focus and deterministic Tab/Shift+Tab cycling
  fixed it. No test was disabled or weakened. Native dialog preserves background inertness.
- Actual owner navigation/admin/image access and logout/revoked-cookie replay passed.
- Web hardened image policy passed, Trivy CRITICAL findings0 including unfixed, SPDX SBOM.

## Activation
Current web: `sha256:685016c7f861ae859386e60689e0b0343979c7b56938f73d270f26102e216a8d`.
Rollback web: `sha256:135c7a20e711a95a3a6bb86efc501b89ce812972bdac7efa3e90ccfb359ce6e9`.
Only web recreated with --no-deps. All14 other container identities/images preserved,
including API73a0fd0 audit fix. HTTPS8443 retained; HTTPS200, HTTP browser308, machine health200.
Private receipts/screenshots/SBOM/rollback: `~/.coire/qualification/025/palettes-*`.
Owned loopback preview server/browser processes and visual dist fixtures retired; no persistent
fixture container/volume, new key, model workload or entitlement mutation created in this slice.
Theme test palettes were browser-context-only and removed before that context closed.

## Constitution
I/II/V: no engine/placement/acquisition path touched. II-a: existing hardened web only.
III: no wire change; client preference types are local validated data. IV: no auth/scope/data
mutation, credentials remain Keychain-backed. VI: existing web/service signals retained; no
new backend path/telemetry destination. VII: amended spec/plan/tasks, meaningful tests and live
WebKit acceptance; no tiny-model test applies to browser color preferences.

## Historical records — superseded visual directions

# Feature025 — literal reference correction,2026-10-16

The owner rejected the previous two interpretations. This record describes the new correction;
the earlier functional/image qualification record is retained below as historical evidence.
No owner visual approval is inferred from tests or screenshots.

## What changed
Replaced the desktop sidebar and card-heavy shell with the supplied site's64px horizontal
masthead, text wordmark, centered672px editorial column, open ruled sections and measured
component styling. Removed custom chat emblem/marketing slogan and split sign-in hero.
Chat suggestions use numbered ruled rows; history is an explicit accessible disclosure at
all widths. Data-heavy sections use the reference's1152px breakout. Preserved existing
image content-mode controls, canonical entitlement and installed API audit fix.

## Direct reference and live comparison
Actual supplied page loaded with scripts/fonts in headless Playwright WebKit26/build2215.
Captured light/dark at1440×1000, then compared actual Coire at identical viewports:

| Property | Reference | Actual Coire |
|---|---|---|
| Heading position | x384,y214 | x384,y214 |
| Heading column | 672px | 672px |
| Heading font | Cormorant Garamond50032px/40px | Same |
| Body | Inter16px/25.6px | Same |
| Masthead height | 64px | 64px |
| Wordmark left | 144px | 143.984px (1/64px WebKit rounding) |
| Light background / heading ink | #faf8f5 / #040810 | Same |
| Dark background / heading ink | #081027 / #e8edf8 | Same |
| Primary button fill / ink | #6b4e1a / #e6cb9a | Same |

Inspected actual reference/Coire screenshots beyond these numeric checks: sign-in, both chat
themes, admin overview/tables, image controls/library, Training, tablet, mobile menu, phone
chat and long conversation/code. Fixed native select sizing, wordmark stacking and gallery
button wrapping. No global horizontal overflow or page exceptions in20 preview scenarios
or18 actual deployed scenarios. Desktop history open/Escape/focus, draft prefill, theme reload,
owner login/navigation/logout and refused revoked-cookie replay verified.

## Delivery
-265 web tests /64 files passed,no skips; strict TypeScript/Vite, ESLint, Prettier and whitespace
checks passed. No API/schema/DB/dependency change in this correction; existing backend gates
and explicit-content qualification remain unchanged.
-Web image hardened policy passed, Trivy CRITICAL findings0 including unfixed, SPDX SBOM.
-Current web: `sha256:22004171a50d6d52b37782cf63183ea293037ce4cce1b36cd811a7ec83683668`.
-Rollback web: `sha256:135c7a20e711a95a3a6bb86efc501b89ce812972bdac7efa3e90ccfb359ce6e9`.
-Only web recreated with --no-deps; all14 other container identities/images retained, including
API audit-fix image73a0fd0. Existing TLS8443 retained; no credential/entitlement changes.
-Private screenshots/measurements, before/after maps and rollback snapshot:
`~/.coire/qualification/025/literal-*`. A side-by-side standalone comparison uses unaltered
reference/live screenshots. No screenshots or test/generated artifacts committed to Git.
-Preview server/browser processes stopped; isolated visual fixture dist files removed;
no persistent test container or volume created.

## Historical qualification before the owner's second rejection

The initial a67ad30 pass was rejected for composition and typography/color/spacing. This
record describes the corrected, locally activated version, following the owner clarification.

## Visual revision and WebKit evidence
- Reinspected the actual Andrei Nita reference in **headless Playwright WebKit26.0/build2215**.
- Matched light paper100/200/400 surfaces, near-black/navy ink and warm gold rather than
  retaining the initial heavy navy sidebar/white cards. Dark mode uses the reference's
  navy800/900 and gold palette. Readable14–16px controls/body, restrained32–42px headings,
  stronger hairlines, aligned spacing and consistent native select rendering in WebKit.
- Images leads with model, prompt, content mode and Generate, before secondary settings.
- Final visual review also fixed the healthy banner: active alerts or stale nodes now prevent
  All systems healthy. A real-ledger-alert regression covers the reachable-host case.
- Inspected actual screenshots: reference light/dark, sign-in, empty chat, long conversation/
  reasoning/code, admin overview/tables, Images, Training, mobile navigation and phone chat.
- Preview:20 scenarios, no page overflow or page exceptions. Deployed WebKit:19 scenarios
  including generated output, then18 final scenarios after the API audit fix. Theme reload,
  draft prefill, mobile Escape/focus, sign-in/logout and revoked-cookie replay passed.
- Screenshots wait for fonts and transitions. Long content uses isolated real-component
  fixtures rather than creating production chat conversations. Actual routes use real data.
- Measured core normal-text/sidebar/button token pairs: minimum5.12:1, both themes. This is
  a token contrast audit, not a claim of exhaustive WCAG certification.

## Explicit image capability — issues101/102
- Fixed admin console's incorrect `explicit_image` grant/revoke target to canonical `explicit`;
  retained audited admin authority and refreshed the current identity after a change.
- Added content_mode selector using existing generated contracts. Fresh and reused requests
  carry mode, explicit presets cannot be downgraded, and restored explicit recipes cannot
  submit without current account access. Backend remains authoritative.
- Owner initially had no explicit grant. Measured explicit submission403, then granted through
  authenticated admin API. The owner's requested entitlement remains enabled.
- Real Studio anatomy job `01M52MTFJNAFMKN25RX966Z1DB` succeeded: nonsexual adult classical
  figure study,512×512/four steps, explicit recipe and tag,274080-byte PNG with matching SHA256.
- Submitted a second adult fine-art anatomy study **through the deployed form in WebKit**:
  job `01M52N35X17GQB5Z1B3TH3AA5P`, output `6f200529-04d1-4dc6-9367-c3ae30a06f83`.
  Verified form POST202 carried content_mode=explicit, Studio completion, explicit tag,
  browser Download action's successful authenticated content response and PNG SHA256.
- Browser harness was corrected to wait for published outputs and use the actual accessible
  Download label. It reused its submitted receipt on retries; no duplicate job was generated.
- Successful grant/submit/dispatch/completion audit rows were verified. Negative testing found
  a pre-existing missing late-refusal audit. Fixed by rolling back admission locks before
  writing the existing independent content-free `image.refused` row. Contract test proves
  rollback→audit ordering and no rejected transaction commit or prompt leakage.
- Live restricted personal key (images scope, no images:explicit) refused403 with matching
  late-refusal audit. Temporary qualification keys revoked immediately; owner access retained.
  No classifier, auth/scope, visibility or entitlement enforcement was weakened.

## Checks and activation
- Web:64 files / **264 tests passed**, no skips; ESLint, Prettier, strict TypeScript/Vite build.
- Backend: **29 relevant authorization/guard/refusal contract tests passed**, no skips;
  Ruff check and format (2395 files), strict mypy (1310 sources), OpenAPI freshness passed.
- Web and API hardened image policy passed, Trivy CRITICAL findings **0** including unfixed,
  SPDX SBOMs generated. No new package dependency, schema, migration or network setting.
  Existing OFL1.1 self-hosted font notices/source hashes retained.
- Current web: `sha256:135c7a20e711a95a3a6bb86efc501b89ce812972bdac7efa3e90ccfb359ce6e9`.
- Current API audit fix: `sha256:73a0fd0f6e9dcc9d0a7a243e02f41651ab2bff38f86516ebec375937cbbb62ee`.
- Web revision rollback: `sha256:d55ed127a05ad8d597ac60859d64735b16420afa5ecb576638c38059fc891253`.
- API rollback: `sha256:8ac5d38c86aaf5067c969b8248d21044f4d9611baf3608aa403346551fd93000`.
- Replaced web, then API for the audit fix, each with --no-deps and its own before/after
  snapshot. All other13 container identities/images preserved. Existing approved TLS8443
  remains; HTTPS200, browser HTTP308, machine health200 and both containers healthy.
- Private qualification screenshots/logs/SBOMs/rollback manifests: `~/.coire/qualification/025/`.
  Generated anatomy images, credentials and test fixtures are excluded from Git.
- Owned preview server and browser processes retired; isolated visual dist fixture removed.
  No persistent test container/volume or native engine outside coire-node was created.

## Constitution
I/II/V: no engine/placement/acquisition change; Studio workloads use registry-owned native
pipeline. II-a: existing hardened separate web/API services. III: generated contracts unchanged.
IV: canonical audited entitlement and refusal evidence; secrets remain Keychain-sourced.
VI: existing route metrics/spans/alerts retained, refusal uses the established content-free
audit helper. VII: amended spec/plan/tasks, regression/contract and real Studio/browser gates.
