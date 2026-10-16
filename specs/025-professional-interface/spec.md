# Feature Specification: Professional Coire interface

**Feature Branch**: `feat/025-professional-interface`
**Created**: 2026-10-16
**Status**: Literal-reference correction locally activated; owner visual acceptance not asserted
**Input**: Follow https://andreinita.co/learning/pydantic-ai/ and professional dashboard/chat references to significantly redesign the local interface.

## User Scenarios & Testing
### US1 — A coherent workspace (P1)
A signed-in user navigates Chat, Images, Training and Admin through a consistent professional shell.
**Independent test**: Traverse all permitted routes at desktop and mobile sizes with keyboard and pointer.
1. Persistent desktop navigation shows the current section; mobile navigation opens/closes accessibly.
2. Existing role/capability visibility, confirmations and mutations remain intact.
3. Page hierarchy, data tables, forms, dialogs, statuses and loading/empty/error states share one visual language.

### US2 — A focused conversation (P1)
A user selects an entitled model, reads messages and composes without a wall of settings competing for attention.
**Independent test**: Choose a model, select a suggested draft, send/stop in the existing workflow and navigate history.
1. Conversation history, model selection, message content and composer have clear hierarchy.
2. Existing chat/code, file, feedback and cancellation functionality stays reachable and accurately labelled.
3. Suggestions fill the draft and never submit or change a model automatically.

### US3 — Comfortable and accessible use (P2)
The reference's warm ivory, navy, muted gold and editorial headings appear consistently, including sign-in. An explicit dark theme is available.
**Independent test**: Toggle theme, reload, sign out/in and verify keyboard/mobile layouts and readable status/disabled states.
1. Theme persists as a non-sensitive preference; credentials and conversation drafts are unaffected.
2. Normal text contrast meets 4.5:1, controls remain keyboard-accessible and reduced motion is respected.
3. Desktop 1440px and phone 390px show no page-level horizontal overflow; data tables can scroll within their own region.

### Edge cases
Unknown cluster state is never labelled healthy. Long identifiers, code, empty rosters, disconnected streams, blocked browser storage, failed requests, disabled actions and non-admin navigation remain understandable.

## Requirements
- FR-001: Replace the floating dock and glass panel composition with a structured sidebar, header and aligned content workspace.
- FR-002: Use the provided reference's palette, typography and restrained editorial style with dashboard density informed by professional product interfaces.
- FR-003: Redesign chat empty state, history, model picker, messages and composer as a coordinated experience; keep functional safeguards and all existing actions.
- FR-004: Apply shared tokens across sign-in, admin, images, training and degraded-state presentation; support explicit light/dark themes.
- FR-005: Preserve authentication, permissions, API contracts, real data, streaming, confirmation and cancellation semantics; no invented metrics or fake controls.
- FR-006: Keep fonts/assets local at runtime; document source/licence for new font assets and add no UI framework dependency.
- FR-007: Update design/runbook documentation and pass existing web tests, meaningful navigation/theme/draft regressions, type/lint/build and real browser visual checks before replacing only the local web image.

## Assumptions and Clarifications
The owner authorizes a significant visual redesign and local deployment on the already approved HTTPS listener. Default light appearance follows the reference; dark mode is explicit. No network, identity, model or backend changes are needed. The new design supersedes the historical Glass mockups. Runtime fonts are self-hosted under their OFL licences. No user-only design choice is unresolved.

## Success Criteria
- SC-001: All four main sections and sign-in use the same palette, typography, navigation and component treatments.
- SC-002: Existing web regression tests pass; added keyboard/navigation/theme and draft-suggestion tests pass with no feature skips.
- SC-003: Real desktop, dark and phone browser screenshots are inspected and corrected; no overlapping navigation/composer or page overflow remains.
- SC-004: Existing owner signs in, navigates actual pages, changes theme and signs out on the deployed local interface; all temporary resources are retired.

## Owner correction — 2026-10-16
The first pass does not sufficiently match the reference. Revise composition and typography,
colors and spacing: use the reference's tonal paper surfaces throughout light navigation,
near-black/navy ink, warm visible hairlines, readable 14–16px body/control text and restrained
32–40px editorial headings. Inspect the actual reference and application using **headless
Playwright WebKit**, including desktop, phone, dark and real operational states. Palette
similarity alone is not acceptance. Preserve existing navigation/controls and user data.

Restore the existing feature015 explicit generation UI path: use the canonical audited
`explicit` admin entitlement, show standard/explicit content mode with entitlement-aware
feedback, preserve explicit preset/recipe requirements, and verify a nonsexual adult human
anatomy study using the real image pipeline. No authorization bypass or schema change.

Live negative acceptance additionally requires issue102's late admission refusal audit fix:
release the rejected request transaction before writing a content-free independent audit row,
retain403 enforcement, contract-test ordering/privacy and verify it live without changing
the owner's entitlement. Existing wire contracts and generated types remain unchanged.

## Owner rejection: literal theme match
The owner rejects both previous interpretations. The supplied site is the styling authority,
not a source of inspiration alongside other dashboard products. Replace the desktop sidebar
and dashboard-card composition with the reference's slim horizontal navigation, centered
672px editorial column, open sections, fine rules, 32px/40px serif headings, 16px/25.6px body,
14px/22.4px supporting text, and its actual button/surface/ink styling in both themes. Dense
operational tables may use the reference's wider1200px breakout, with the same heading and
component system. Sign-in must share that structure rather than a split marketing screen.
Compare matching-viewport screenshots and measured styles in headless Playwright WebKit.
Passing functional tests or matching several colors does not establish visual acceptance.
