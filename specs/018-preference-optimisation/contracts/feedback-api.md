# Feedback, Comparison and Export Contracts

These are design contracts; implementation must define every wire type in `coire_core.models.feedback` before routes, then regenerate OpenAPI/TypeScript. No handwritten OpenAPI or TS response types. All datetime values are UTC, bodies forbid extra fields, errors are CoireError-derived RFC 9457 problems with bounded reason codes and no source text.

## Authentication, versioning and common behavior

Owner endpoints use `CurrentChatUser`: authenticated active human owner, ordinary user API keys require `chat`, browser mutations use existing same-origin checks. Admin role does not bypass conversation ownership. Service/run credentials are refused. Admin endpoints require the same live active-user/admin/key-version and same-origin policy as training admin routes. All reads revalidate authority and contributor eligibility.

Owner mutations carry `client_request_id: UUID`; editing a row carries `expected_version >= 1` (new thumb uses expected_version 0). Comparison creation additionally carries conversation `expected_revision`. Admin mutations use required `Idempotency-Key` length 1–128 plus expected_version for edits. The identity/body hash is immutable: identical authorized replay returns existing status; mismatched body is 409. Recheck current privacy before receipt replay and return a tombstoned projection when withdrawn, never a cached copied answer. Commit accepted mutation, audit and receipt atomically; refusal audit uses a separate safe transaction. A missing or foreign conversation/message is 404; stale version/conflicting lifecycle 409; inactive capture 403; malformed request 422; quota/budget limit 429; unavailable exact target 503 with normal retry semantics.

Each route below requires contract coverage for success, unauthenticated, missing scope, inactive/revoked principal, foreign identifiers, stale/versioned replay and bounded errors where applicable.

## Owner routes

| Method and path | Request | Response / behavior |
|---|---|---|
| GET `/api/v1/chat/feedback-preference` | none | `FeedbackPreference` incl. disclosure/version, enabled default and capture generation; no content |
| PATCH `/api/v1/chat/feedback-preference` | `FeedbackPreferenceUpdate{client_request_id,expected_version,enabled,disclosure_version}` | Current setting; first materialization has version 1 exposed by GET; disable invalidates generation atomically, schedules purge and stops pending contribution generation |
| GET `/api/v1/chat/conversations/{cid}/feedback` | cursor?, limit 1–100 | `ConversationFeedbackPage` current thumbs/pair statuses and eligibility reasons for loaded messages; no historical withdrawn copies |
| PUT `/api/v1/chat/conversations/{cid}/messages/{mid}/feedback` | `ThumbUpdate{client_request_id,expected_version,judgement:up|down|null,tags}` | `FeedbackReceipt`; owner/completed-assistant-only; nullable judgement clears current thumb |
| POST `/api/v1/chat/conversations/{cid}/comparisons` | `ComparisonCreate{client_request_id,expected_revision,source_message_id}` | 202 `ComparisonReceipt{id,version,state,events_path}`; one pending pair, source latest and eligible, no caller prompt/model strings |
| GET `/api/v1/chat/conversations/{cid}/comparisons/{pair_id}` | none | `ComparisonDetail`: original/candidate visible text only if eligible, status, usage, exact recorded target, current owner judgement and version |
| POST `/api/v1/chat/conversations/{cid}/comparisons/{pair_id}/selection` | `ComparisonSelect{client_request_id,expected_version,candidate:original|candidate,tags}` | `ComparisonSelectionReceipt` with pair/feedback and conversation revisions; pending pair chooses active answer; later rejudgement changes feedback only |
| POST `/api/v1/chat/conversations/{cid}/comparisons/{pair_id}/dismiss` | `ComparisonDismiss{client_request_id,expected_version}` | Cancel owned regeneration if needed, preserve original, release pending lock and return current state |

No separate unbounded history endpoint. Current feedback page supports reconnect and reload without inferring state from DOM. Tags are ≤16 distinct slugs, length 1–32, `[a-z0-9][a-z0-9-]*`; tags are labels only, never instructions or telemetry labels. Setting changes carry the current disclosure version and retain it in owner state; all contribution responses expose the active disclosure text/version. The UI fetches and visibly renders the disclosure before enabling contribution controls; no additional consent-click flow is introduced.

## Comparison execution, transcript and events

Source must have a prospective exact prompt/serving snapshot from an eligible local text execution under the current enabled capture generation. Check the whole prompt: prior file-derived text, tool/code/harness or image content makes it ineligible. Historical incomplete provenance gets reason `source_provenance_unavailable`; ordinary chatting/thumbs still works. Native exact resolution/cold loading pins actual variant/manifests and rechecks entitlement/publication before regeneration. An adapter target is only usable if already resolved and authorized through registry-backed serving; this feature does not add an adapter picker or guess one from a string.

Keep original answer active while candidate executes. Freeze generation settings/seed policy from the source snapshot, but allocate a fresh recorded sampling seed for regeneration (temperature-zero runs may be identical and then are ineligible). Selection changes the active answer mapping only while no subsequent turn exists and the captured context revision still matches. Later feedback changes do not rewrite history. A pending pair blocks context-changing send/model changes with 409 `comparison_pending`; rename may proceed without invalidating the separate context revision. Failed/cancelled/identical/expired regeneration unlocks the conversation. Dismissal is idempotent and stops owned work. Owner opt-out/deletion takes precedence over all later deltas/completions.

Reuse existing conversation SSE endpoints and `useEventStream`: typed events `comparison.accepted`, `comparison.status`, `comparison.delta`, `comparison.ready`, `comparison.selection`, `comparison.terminal` have pair ID/version and cursor; reset snapshots include pending/current pair metadata. Replay and detail reads recheck capture generation and tombstones. SSE-stored candidate bytes follow withdrawal purge. Limit candidate output to 64 KiB; overflow terminates with a typed reason and never truncates into an exportable answer. Browser reconnect fetches current state; never starts a second generation. Existing chat accounting charges regeneration exactly once even when requests/replay duplicate. No raw content in command receipts, DBOS args/results, logs or audit.

## Admin review routes

| Method and path | Request | Response / behavior |
|---|---|---|
| GET `/api/v1/admin/feedback/comparisons` | cursor?, limit 1–100, state=unreviewed|reviewed|skipped | `FeedbackReviewPage`; complete eligible explicitly generated pairs only; stable `(created_at,id)` order |
| GET `/api/v1/admin/feedback/comparisons/{pair_id}` | none | `FeedbackReviewDetail`; bounded frozen prompt and visible answers, source/target provenance, current owner/admin decisions and version |
| PUT `/api/v1/admin/feedback/comparisons/{pair_id}/judgement` | `AdminPairJudgement{expected_version,choice:original|candidate|skip,tags}` | Current judgement receipt; active admin and owner eligibility rechecked; skip is not a label and remains revisitable |

One current admin judgement per pair with actor/version; optimistic conflict prevents lost updates. User and admin choices remain separate. No queued/rejected/withdrawn/dismissed/expired pair is reviewable. Review never alters the owner's active chat answer. Review of unchosen `ready` pairs is allowed while their 24-hour pending lifetime remains; export needs a judgement, not necessarily an owner selection, but capture/deletion/expiry fences still apply. Admin judgement does not release the owner's pending context lock. After pair expiry/dismissal, its unexported review contribution is withdrawn too.

## Admin export routes

| Method and path | Request | Response / behavior |
|---|---|---|
| POST `/api/v1/admin/feedback/exports` | `PreferenceExportCreate` below | 202 `PreferenceExportReceipt{id,state,version}` |
| GET `/api/v1/admin/feedback/exports` | cursor?, limit 1–100 | `PreferenceExportPage`; bounded content-free progress/history |
| GET `/api/v1/admin/feedback/exports/{export_id}` | none | `PreferenceExportDetail`: state/counts/exclusions/warnings/deadline/dataset/cleanup state |
| POST `/api/v1/admin/feedback/exports/{export_id}/cancel` | `PreferenceExportCancel{expected_version}` | Idempotent cancel receipt before publication; 409 with dataset ID after publication |

`PreferenceExportCreate`: dataset `name` (1–120), `license_note` (1–2048), analysis `model_id`/`variant_id`, `split_seed`, `validation_fraction` (0,1), `filters{model_id?,variant_id?,adapter_id?,owner_id?,tag?,from?,until?}`, `source=owner_preferred|owner|admin` default owner_preferred. Require from < until if both; variant/adapter must belong to supplied model if present. Resolve these as exact registry IDs; metadata/provenance snapshots permit retired generating models in exports, but the analysis model must be currently usable. No public signed download, arbitrary filename or external URL.

Default chooses latest owner pair judgement or otherwise latest admin choice; apply date/tag filters to that effective judgement. Source-only modes choose the corresponding latest judgement. At most one pair row, stable sort by pair ID; duplicate/identical-invalid examples diagnosed by normal dataset validation. Thumbs excluded. Membership freezes source IDs/versions and records exact identities/actor/source without embedding extra source conversation bytes.

Execution uses private quota and existing dataset store; maximum 10,000 pairs and 256 MiB, 100 queued and one active export. Oversize selection fails with counts/limit rather than silently exporting a subset. Queue timeout one hour, execution publication deadline five minutes, ≤3 rebuilds on eligibility/source change. Zero rows yields a typed failed export; 1 prompt group cannot become a ready dataset. Fewer than 20 pairs warns. Publication revalidates live admin/key authority and sorted owner/conversation/judgement locks; a withdrawn contribution is removed even if snapshotted earlier. Published registration fixes the privacy boundary even if token analysis is still pending. On commit uncertainty, recover by export/dataset identity. Cancel/restart must not delete committed data or release quota for unremoved bytes.

## Preference dataset/CLI/UI reuse

Existing `/api/v1/admin/datasets` upload/analyze/detail/retire and `coire data` commands accept `format=preference` with versioned typed rows and prompt-group split/analysis identities. Existing training recipe/form accepts v3 (see companion contract). Add `coire feedback export`, `coire feedback exports`, `coire feedback export-show` and `coire feedback export-cancel`; these call authenticated admin API operations and never read chat databases directly. Export submission prints the ID; history/detail shows warnings, dataset readiness and cleanup. Admin console uses the same endpoints; no browser-side export assembly.

Chat shows disclosure, persistent capture toggle, thumb state and comparison controls with keyboard focus and `aria-live` status. Training admin shows review/export form/history, objective configuration and dataset warnings; all reads use generated types and `src/api/`, streaming uses shared hook. Explain in ordinary language: withdrawal removes unexported contributions; already published datasets and trained models remain. Do not display engine argv, token masks or implementation versions in ordinary user flows.

Native local text turns optionally accept strict bounded `NativeChatSampling`. The chat interface offers explicit “Varied answers” (temperature 0.7/top-p 0.95); omitted sampling preserves legacy engine defaults and request hashes. Captured provenance freezes the actual admitted settings, and regeneration changes only the recorded seed. Sampling is refused for remote/visual backends and non-chat actions.
