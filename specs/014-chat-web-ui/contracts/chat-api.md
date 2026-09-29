# Chat contracts

Design contract for strict `coire-core` Pydantic types. Generated OpenAPI and `apps/coire-web/src/api/schema.d.ts` become the executable wire authority during implementation. Existing `/v1`, MCP and node contracts retain compatibility.

## Identity and errors

Every `/api/v1/chat` route requires a user-bound Access identity or user-owned API key with `chat` scope. Reject anonymous, run-token, node, ops and unowned service identities. Ownership applies to admins too. Retain rate limits/budgets and once-only gateway usage accounting. Cookie-authenticated mutations require the configured exact same-origin browser request; reject cross-origin requests without widening CORS. Bearer clients retain scoped authentication.

Missing, foreign and deleted objects return uniform 404s. Other statuses: 401 missing authentication; 403 wrong credential/scope; 409 revision, active-turn, idempotency or unsafe-state conflict; 413 byte quota; 415 unsupported file content; 422 invalid input/context; 429 rate/budget refusal; 503 unavailable admission with `Retry-After` when known. Domain failures use `CoireError` subclasses and existing problem details. Failures after SSE headers become typed error/terminal events, never raw engine exceptions.

## Endpoints

Paths are relative to `/api/v1/chat`. JSON/SSE models live in `models/chat.py`; canonical content in `models/conversation.py`.

| Method and path | Request | Response / behavior |
| --- | --- | --- |
| GET `/models` | `ChatPickerQuery`: mode/action | `ChatPickerResponse`: published, ready, entitled entries, user-safe fields. Read actions allow unverified models; Apply indicates write eligibility. Refresh every picker open. |
| GET `/conversations` | `ChatPageQuery`: opaque cursor, limit 1..100 (default 50) | `ChatConversationPage`, newest updated first with stable `(updated_at,id)` cursor. |
| POST `/conversations` | `ChatConversationCreate`: optional title, mode/model | 201 `ChatConversation`; owner exclusively from principal. |
| GET `/conversations/{id}` | `ChatMessagePageQuery`: before_position, limit | `ChatConversationDetail`: metadata, message/turn page, active turn, event cursor and attachment summaries. |
| PATCH `/conversations/{id}` | `ChatConversationUpdate`: expected_revision, optional title/mode/model | `ChatConversation`; mode/model changes conflict during active generation. Later turns record their model independently. |
| DELETE `/conversations/{id}` | `ChatDeleteRequest`: expected_revision | 202 `ChatDeletionResult` with purge deadline; immediate tombstone and cancellation. Repeated owner deletion is idempotent. |
| POST `/conversations/{id}/turns` | `ChatTurnCreate` below | 200 SSE `ChatEvent` after admission. First event identifies persisted turn/messages. Original request controls disconnect cancellation; duplicate identity/body only replays/follows existing turn. |
| GET `/conversations/{id}/turns/{turn_id}` | Owner and parent binding | `ChatTurnDetail`: state, saved content/cursor, usage, optional typed coding result. |
| POST `/conversations/{id}/turns/{turn_id}/stop` | `ChatStopRequest`: reason `user_stop|navigation` | `ChatTurn`; idempotent. Revoke coding token before queued kill; release inference resources and preserve partial text. Report stop requested until acknowledgement. |
| GET `/conversations/{id}/events` | Optional `Last-Event-ID` | Read-only observer/replay SSE; never starts/retries/keeps alive/cancels generation. Snapshot and cursor are atomic. |
| POST `/conversations/{id}/files` | Multipart `ChatUploadMetadata` plus opaque bytes | 202 `ChatAttachment` in processing state; strict metadata and bounded original staging/quota/digest validation, followed by isolated processing and attachment events. Text-only fast path may complete in the same request. |
| GET `/conversations/{id}/files/{file_id}` | Owner and parent binding | `ChatAttachment`; no storage path. |
| GET `/conversations/{id}/files/{file_id}/content` | Owner and parent binding | Authenticated bytes, attachment disposition, escaped filename, `nosniff`, `no-store`; no inline document or bearer URL. |
| DELETE `/conversations/{id}/files/{file_id}` | `ChatFileDeleteRequest`: expected_revision | 202 `ChatDeletionResult` for unreferenced uploads; 409 if referenced in a saved message. Composer removal only changes the unsent selection. |
| POST `/conversations/{id}/files/{file_id}/process` | `ChatFileProcessRequest`: request_id, expected_revision, operation, selected pages/mode | 202 `FileProcessJob` for explicit retry or selected-page rendering; same request ID is idempotent, unchanged failed bytes permit at most two attempts. |
| GET `/conversations/{id}/files/{file_id}/assets/{asset_id}` | Owner and file/job binding | Private normalized image/page thumbnail bytes only, safe raster MIME and no-store/nosniff. Original PDF is never embedded. |
| GET `/conversations/{id}/turns/{turn_id}/artifact` | Owner, active conversation, Apply result | Existing digest-verified branch artifact under 64 MiB / seven-day limits. Expiry is explicit. |

`ChatTurnCreate`: `client_request_id` UUID, `expected_revision`, `model_id`, `content` <=64 KiB, `attachments` <=10 strict `ChatAttachmentSelection` values (file_id, text/visual mode, explicit selected PDF pages), action `chat|research|plan|apply`, optional `retry_of`, optional existing workspace/revision and plan/research references. All selected assets must be processed/ready and authorized; visuals require measured model capability, with verification additionally required for Apply. Retry retains original input/selections; conflicting replacement content is refused but model may change if eligible. Plans match source/resolved revision. No caller path, URL, remote credential or engine option.

Picker UUIDs are opaque transport identities. Display name, description, tags, context, size class, verification, accepted text/image modalities and limits, load state and nullable measured estimate are visible. Node/variant IDs, ports and placement are absent. Incompatible models are explained for the current attachment selection; non-entitled models remain absent. No compatible model yields guidance to remove/change inputs or contact an admin, never acquisition.

## Streaming and consistency

`ChatEvent`: conversation_id, cursor, nullable turn_id, type, created_at and discriminated typed payload. IDs are `<conversation_uuid>:<cursor>` and clients deduplicate. Native event shapes do not alter `/v1` chunks.

| Type | Payload |
| --- | --- |
| `snapshot` | Current metadata, bounded message page/active-turn content, cursor and replacement flag. |
| `conversation.updated` | Revised metadata and safe reason. |
| `turn.accepted` | Turn/input/assistant IDs, revision, selected model snapshot. |
| `turn.status` | Actual state, nullable estimate/queue position and safe explanation. |
| `message.delta` | Assistant ID, channel `answer|reasoning`, append text and resulting offset. |
| `run.activity` | Validated `RunActivity` metadata. |
| `run.activity_status` | Final `complete`, `truncated` or `unavailable` spool state and last durable sequence. |
| `turn.result` | Existing typed research/plan/apply result, bounded diff/citations/test outcome/artifact metadata. |
| `turn.terminal` | Completed/failed/stopped/interrupted state, final usage/lengths and optional safe problem. |
| `attachment.changed` | Safe metadata or removed ID. |
| `conversation.deleted` | ID/revision only, then close observers. Tombstoned content cannot be replayed. |

Persist before emitting. Flush first content immediately; subsequent coalescing is bounded to 20 ms. Message snapshots survive 24-hour event compaction. Expired cursors receive replacement snapshots; foreign/future cursors are 409. Recheck identity validity/tombstone at least once per second and before emitting content batches. GET observers reconnect with bounded backoff; POST transport failure never restarts inference. Recover saved state with GET and offer explicit retry/continue.

Hidden tabs retain their controlling connection. Navigation/closure aborts it and sends best-effort owner stop; server disconnect cancellation is the fallback. Observer/duplicate-POST disconnects have no cancellation authority. Healthy explicit Stop completes within five seconds. Each turn has an independent once-only usage key; replay and stop/complete races cannot charge twice. API crash expires plain-chat leases into interrupted state; durable coding runs reconcile from scheduler and can be viewed/stopped after recovery.

## Files and context

Support UTF-8 `.txt`, `.md`, `.csv`, `.json`, `.yaml`, `.yml`, `.toml`, `.xml`, `.html`, `.css`, `.js`, `.jsx`, `.ts`, `.tsx`, `.py`, `.rs`, `.go`, `.java`, `.c`, `.h`, `.cpp`, `.hpp`, `.sh`, `.sql`, extensionless text, PDF, and still PNG/JPEG/WebP. Validate decoded content independently of MIME/extension. Reject invalid UTF-8 in text, archives, encrypted/malformed PDF, animated images, unsupported formats, decompression bombs and bounds violations. HTML/code stays quoted input/download. Images normalize orientation/color/size and strip derived metadata while originals remain private. PDF text has page attribution; selected page images support scans/diagrams. Mode/pages are visible; an empty text layer cannot pass as a successful text document. Refusals name limits and remedies.

Use FR-026/036/038 and data-model quotas, counting original and derived bytes. nginx upload-only body limit permits bounded overhead: 11 MiB for default 10 MiB originals. API enforces exact streaming bounds, reserves aggregate output quota and cancels processing on deletion. When selected PDF pages require rendering, Send remains disabled until assets are ready. A context/image-limit error never silently omits pages.

Compile full selected history, text/page content, ordered images, framing and response allowance. Count image tokens by validated backend/model allowance, not base64 text length; enforce per-model count/pixel/encoded-byte and memory limits. Label estimates approximate and preserve engine refusal. Switching to an incompatible model is blocked while historical visual inputs remain in the selected context; offer an eligible model or new conversation. No implicit summarization/drop.

## File-worker boundary

`POST /v1/process` on the private worker accepts strict `FileProcessRequest`: opaque job/input IDs, immutable SHA-256, operation, selected pages, bounded output IDs and deadline. A Keychain-sourced service credential scopes the scheduler caller to processing; every worker route including health requires its own scoped credential. Worker has no public ingress, database, Studio or external network access. Worker reads generated input keys on a read-only original mount and writes only its derived-job mount; request data never supplies raw paths. `FileProcessResult` returns detected type, page/dimension metadata, per-page text/extraction state, output asset digests/sizes and safe failures. API checks IDs/digests/quotas before publication.

One conversion/process, 512 MiB/1 CPU, 30-second hard deadline. A watchdog terminates stuck native work by ending the sole worker process; container restart and scheduler DBOS job reconciliation yield a visible failure, not an automatic infinite retry. Limits: 50 PDF pages inspected, 20 MP upload decode, normalized <=2048 px/side and <=4 MP, 1 MiB extracted text and 32 MiB derivatives/job. Test malformed inputs, worker death, duplicate jobs, foreign IDs, output tampering and cleanup.

Authenticated `GET /v1/jobs/{job_id}` returns `FileProcessStatus` with immutable result manifest or running/failed/missing status; authenticated `POST /v1/jobs/{job_id}/cancel` accepts `FileProcessCancel` and returns the status. Job identity and source digest are immutable and repeated process requests cannot rerun completed/failed bytes. Scheduler DBOS owns orchestration and uses this status after restart; API performs final manifest verification before ready publication. A cancellation flag prevents publication and the hard deadline bounds any uninterruptible native call; pending jobs cancel immediately. Health uses a strict shared health response and scoped worker credential.

## Visual inference and coding boundaries

Add `EngineBackend` and validated modality/visual limits to registry/acquisition/start/status contracts; existing values default to mlx_lm/text. Admin-only acquisition initially accepts already converted supported MLX-VLM repositories; unsupported conversion recipes fail explicitly. Validate local processor/config completeness and an actual visual smoke, then replicate/verify both copies. Never let a chat request fetch model data.

OpenAI `content` accepts existing string/null or strict `text`/`image_url` parts. Only bounded inline data images are accepted from `/v1`; HTTP/file URLs are refused before routing. External inline data uses the same isolated normalization/limits via temporary private processing jobs, cleaned after the request. Native Chat resolves owner-authorized assets to normalized data URIs. Preserve existing text `/v1/messages` behavior and explicitly refuse unsupported image blocks there rather than silently dropping them. All Coire-specific public response fields are prefixed `coire_`.

Node launches explicit backend-specific argv for registry-local paths. VLM uses `python -m mlx_vlm.server`, bounded `--max-num-seqs`, `--vision-cache-size`, and `--max-kv-size`; never pass mlx-lm-only `--chat-template`, and refuse unsupported registry template overrides rather than ignore them. Set `HF_HUB_OFFLINE=1`, remove HF credentials and inherited `MLX_TRUST_REMOTE_CODE`, and never enable remote code. Reserve vision encoder/cache/working memory through the existing ledger and leases, single-node placement only initially. Record/re-adopt/stop VLM processes using existing PID/create-time ownership; health and cancellation require local integration evidence.

Extend `HarnessMessage`/`HarnessRunRequest` with typed bounded visual parts; stage them in a dedicated read-only control-input mount and preserve them through coding retries/context handling. The actual `GatewayTransport` path emits OpenAI content arrays over the existing relay; no model payload in environment/logs and no token-bearing asset URLs. Visual coding requires visual plus ordinary coding capability; Apply still requires verified model admission and launch.

## Node activity

Add authenticated `GET /runs/{run_id}/activity?after_sequence=N&limit=100` returning strict `RunActivityPage`. Read only the assigned run output spool; validate run ID/tool enum, bound bytes/records and report truncation/unavailability. Agent emits started/completed/failed around actual tool calls, capped at 10,000 records / 2 MiB, without user content. Scheduler collects at most once/second and drains before cleanup, deduplicating run+sequence. No new port, run egress, relay route or Docker capability. Older nodes report unavailable and require compatible versions for 014 acceptance.

## Contract test inventory

Cover every route, status, request/response and event; admin-as-other-owner; revoked entitlement/session; cross-parent IDs; duplicate/body mismatch; stale edit; deletion through alternate artifact routes; expiry; cursor compaction; cancellation races; missing estimates; multipart traversal/UTF-8/concurrent quota races; node malformed/overflow activity. Preserve `/v1`, MCP and admin snapshot compatibility.
