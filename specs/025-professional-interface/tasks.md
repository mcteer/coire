# Tasks: Professional interface

## Setup
- [x] T001 Specify, clarify and plan the owner-directed theme and professional dashboard/chat research.
- [x] T002 Add licensed self-hosted fonts and shared light/dark tokens; update canonical design documentation.
## US1 — Workspace
- [x] T003 Rebuild AppShell with persistent navigation, page headers, role-aware admin sections and accessible mobile drawer; add meaningful navigation tests.
- [x] T004 Reskin actual admin/Images/Training tables, forms, statuses and empty/loading/error states consistently.
## US2 — Conversation
- [x] T005 Redesign history, model selection, message content, empty state and composer without changing inference or safety controls; verify suggestion-prefill behavior.
## US3 — Accessibility and themes
- [x] T006 Add persisted guarded theme preference, explicit toggle and polished sign-in; test theme and storage failure paths.
- [x] T007 Inspect real desktop/mobile/light/dark screenshots and fix layout, long-content, focus and contrast issues.
## Delivery
- [x] T008 Pass web tests, build/types, lint/format, image policy/critical scan/SBOM; update UI operations runbook and evidence.
- [x] T009 Deploy only validated web image with rollback on existing local TLS; verify actual owner walkthrough, document results and remove all fixtures.

Dependencies: T001 → T002 → T003/T004 → T005/T006 → T007 → T008 → T009. Presentation changes reuse existing typed contracts and service observability. Independent lint and browser checks may run concurrently; no sub-agent work is requested.
