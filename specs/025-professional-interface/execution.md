# Feature025 execution — owner revision, 2026-10-16

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
