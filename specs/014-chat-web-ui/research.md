# Research: Chat Web UI

**Date**: 2026-09-28 | **Base**: `55ca455` | **Spec**: [spec.md](spec.md)

## Existing platform and integration gaps

| Area | Evidence in the repository | Decision |
| --- | --- | --- |
| Ordinary-user entry | `apps/coire-web/src/App.tsx` rejects non-admins and owns a private admin Shell | Extract a role-aware shell and add Chat routes without subscribing ordinary users to privileged console data. Preserve `main.tsx` failover dispatch. |
| Inference | `coire_api/routes/v1.py`, `gateway/{resolution,loading,context,proxy,usage}.py` already authorize, load, stream, cancel, and account | Extract reusable execution from the route; Chat and `/v1` use the same admission, limits, lease and usage path. No HTTP loopback and no additional model runner. |
| Eligibility | `registry/service.py:visible_to` rejects entitlement-bearing models; `gateway/resolution.py:_visible` compares entitlements with scopes; coding selection already uses principal entitlements | Consolidate eligibility and test both list and send. Chat additionally requires published/ready even for admin users. Preserve deliberate admin behavior on existing non-chat routes. |
| Picker | `models/registry.py:ModelListing` has nullable measured warm-up duration and internal `loaded_on` fields, but no size class | Add an owner-safe picker projection and derived size class. Unknown estimates stay unknown; registry readiness differs from engine residency. |
| Transcript | Only `gateway.ChatMessage` and ops-specific conversations exist | Introduce canonical strict content contracts plus owner-scoped persisted chat projections. Do not change the permissive OpenAI compatibility boundary into a strict native contract. |
| Coding | `mcp_calls.py`, `runs.py`, scheduler runs, node workspaces, agent coding and MCP artifacts supply 013 lifecycle | Share domain operations under separate chat and MCP authorization adapters. MCP remains API-key-plus-`mcp` scoped; browser Chat uses its own user scope. |
| Activity | Existing run events show lifecycle only; final structured results arrive at completion | Add a bounded typed activity spool in the existing run output mount. Node reads it, scheduler collects it while waiting, API persists owner events. No new run-container network route. |
| Streaming UI | `useEventStream` assumes GET snapshots, retries EOF, and aborts hidden tabs; parser assumes one LF `data:` line | Extend the shared hook with explicit snapshot/turn modes. POST generation is never automatically repeated. GET observer/replay connections never control execution. |
| Files | No general upload store; nginx has no explicit upload limit | API owns durable originals and metadata; a separate bounded CPU file worker reads originals and writes derived text/rasters under generated job keys. API serves owner-authorized downloads/previews and sweeps tombstones. |
| Vision | `coire_node/engines.py` launches only `mlx_lm.server`; gateway and harness content are text-only | Add direct `mlx_vlm.server` support on Studios, typed content parts, measured visual capability, and a backend discriminator through admin acquisition/lifecycle. A vision tag alone is insufficient. |
| Schema baseline | Alembic head `0014_mcp_calls` | One reversible migration `0015_chat_conversations`; verify the head again at implementation time. |

## Decisions, rationale, alternatives

### 1. Persistent turns with a controlling stream

**Decision:** A POST admits and persists a turn before inference. The sending POST owns a stream; an ordered persisted event log and message snapshots support separate read-only GET replay. Persist content before emitting it. A unique `(conversation_id, client_request_id)` prevents duplicate generation; a locked conversation revision and active-turn constraint serialize new turns. Explicit retry creates a new attempt linked to the original input, and Continue creates a new user turn including the saved partial answer in context.

**Rationale:** This directly satisfies partial history, two tabs, cancellation on leaving, and no accidental regeneration. A service restart marks stale plain-chat attempts interrupted. Existing DBOS coding runs retain their independent durable lifecycle and are reconciled into the transcript.

**Alternatives:** Browser-only transcripts lose history and cannot enforce ownership. Automatically reissuing POST on reconnect spends twice. A new durable plain-chat workflow would add machinery without a requirement to continue generating after the user leaves.

### 2. Bounded content and private files

**Decision:** The user selected text, code, PDFs and images. Use documented original/derived quotas and a separate CPU file worker for PDFium/Pillow processing. Plain-chat context overflow is blocked with corrective options. History remains until deletion; tombstones deny access immediately and cleanup removes originals, derivatives and content within 24 hours. Use environment settings for bounds.

**Rationale:** Expanded attachment support is an explicit user choice. Core may perform CPU parsing, but native parser crashes must not take down the gateway and Principle II-a prohibits an extra API subprocess. The worker has its own single process, one conversion at a time, no ML runtime, and a 512 MiB/1 CPU budget. Approximate text preflight is labelled; visual budgeting uses measured registry limits and backend preprocessing bounds, with the engine's refusal authoritative. No model weights/tokenizers on core.

**Alternatives:** Text-only uploads were declined by the user. An API child parser process conflicts with literal II-a; parsing inside the API process shares its crash/memory fate. A new general blob service is unnecessary: the dedicated worker only preprocesses bounded files. Retrieval, automatic plain-chat summarization and a separate OCR engine remain out of scope.

### 3. Code mode reuses explicit coding actions

**Decision:** Research, Plan, Apply use the approved repository/revision and fresh workspace preparation from 013. A chat adapter records conversation/turn and shares domain services. A prior same-owner Plan can feed Apply only for matching source/revision. Return diff/test outcome/expiring artifact. Extracted text and normalized image parts enter the coding prompt; image parts require a visual-capable coding model, with verification for Apply. Stage trusted request data in a separate read-only control-input mount, not the editable repository.

**Rationale:** It reuses the verified-write gate, artifact lifecycle and kill switch already proven in 013. The UI explicitly shows that the next action starts from the selected revision.

**Alternatives:** Persistent mutable workspaces need additional ownership, scheduling, recovery and conflict rules. Calling the MCP HTTP service from Chat would couple availability and require the wrong credential scope. Renaming all MCP persistence during this feature creates unrelated migration churn; add an origin discriminator instead.

### 4. Shared authenticated event transport

**Decision:** Support UTF-8 chunk boundaries, CR/LF framing, comments, event names, multiline data and event IDs in `api/eventStream.ts`. The hook resets cursors on conversation changes, distinguishes terminal/auth states, and keeps active generation alive when hidden. Reconnect only observer GETs; after a sending stream fails, retrieve persisted state and offer explicit retry/continue.

**Rationale:** The current admin snapshot mode is reusable but does not satisfy generation semantics. Protocol framing follows the [HTML event-stream standard](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream). Fetch streaming and AbortController are documented in [MDN's Fetch guide](https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch).

**Alternatives:** A second chat-only SSE parser duplicates error-prone transport logic. Native EventSource cannot start the POST operation. WebSockets add a transport the platform does not need.

### 5. Safe rich text with one focused dependency

**Decision:** Plan `react-markdown==10.1.0` (MIT), with raw HTML disabled, an explicit HTTP/HTTPS link policy, and image rendering replaced with inert text. No raw-HTML plugin or syntax-highlighting dependency. Lock both npm and pnpm graphs because CI/Docker use npm while documented developer commands use pnpm.

**Rationale:** A maintained React renderer avoids an improvised Markdown parser and HTML injection. The [official manifest](https://raw.githubusercontent.com/remarkjs/react-markdown/10.1.0/package.json) confirms the release, MIT licence and React >=18 peer requirement; the [official README](https://github.com/remarkjs/react-markdown) documents renderer and URL customization. Revalidate the lockfile/security scan during implementation.

**Alternatives:** Plain text fails the designed code/list rendering. A custom renderer expands maintenance/security testing. Existing multipart support must be declared directly at its already locked version if imported directly; additional worker/node packages are listed below.

### 6. Bounded PDF and image processing

**Decision:** Pin `pypdfium2==5.13.0` and `Pillow==12.3.0` in `apps/coire-file-worker`. PDFium handles both Unicode text extraction and selected-page rasterization; Pillow validates, orients and normalizes still PNG/JPEG/WebP. Use standard non-V8/non-XFA wheels, one serialized conversion, 30-second hard wall deadline, 512 MiB memory/1 CPU and bounded input/output. A worker watchdog terminates its one process on a stuck native call; compose restarts it and the scheduler marks the durable job failed. Retry is explicit, capped at two attempts for unchanged bytes.

**Rationale:** PDFium is explicitly not thread-safe, so jobs never run concurrently in this worker. Extract text with page attribution; selected page images permit scans/diagrams without a separate OCR engine. Normalize to <=2048 px per side and <=4 megapixels, strip metadata from derived images, retain originals privately, and cap derivative output at 32 MiB/job and extracted text at 1 MiB/job. Bounds produce visible errors/page-selection requests, not silent omissions.

**Licences and evidence:** PDFium binding Apache-2.0/BSD-3-Clause plus bundled PDFium/third-party notices; Pillow MIT-CMU. Preserve wheel notices/SBOM and verify native libraries in arm64 distroless runtime. Both releases supply appropriate Python 3.13/Linux arm64 or compatible wheels. Sources: [PDFium release](https://pypi.org/project/pypdfium2/5.13.0/), [threading/text API](https://pypdfium2.readthedocs.io/en/stable/python_api.html), [licensing](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing), [Pillow release](https://pypi.org/project/pillow/12.3.0/), [image bounds](https://pillow.readthedocs.io/en/stable/reference/Image.html), [orientation](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html).

**Alternatives:** pypdf duplicates the required PDFium text function and does not rasterize scans. External Ghostscript/Poppler/OCR binaries add unnecessary runtimes. Original PDF embedding in the browser would bypass safe raster previews.

### 7. Bare visual inference

**Decision:** Add registry-controlled `mlx_vlm.server` on Studios, initially pin `mlx-vlm==0.7.3`, and retain `mlx_lm.server` for text models. Native gateway/node/harness contracts carry bounded standard text/image content parts; only registry-resolved local model paths reach the launcher. Reject arbitrary image URLs; feed private normalized data URIs through existing authenticated inference routes. Extend admin acquisition/validation/replication to vision models, measuring image support and bounds before publication.

**Rationale:** The official bare server supports OpenAI chat content arrays and streaming. This satisfies images without an inference wrapper, core model execution, or an external vision service. Architecture documentation and a scoped ADR will record the new engine and CPU-worker roles. No constitutional exception is needed: model ownership/lifecycle and container constraints remain enforced.

**Deployment gap:** The current installer uses `uv pip install --upgrade` from wheel metadata, so a lockfile alone does not pin deployed engines. Stage locked dependencies into a new versioned node environment, validate import/health/text+visual smoke, then use the existing symlink flip/rollback pattern. This is the reproducible install needed for 014, not the feature-019 admin upgrade-job UI.

**Evidence:** [official MLX-VLM](https://github.com/Blaizzy/mlx-vlm) and [v0.7.3 source](https://github.com/Blaizzy/mlx-vlm/tree/v0.7.3). A candidate local tiny fixture is [SmolVLM-256M-Instruct-4bit](https://huggingface.co/mlx-community/SmolVLM-256M-Instruct-4bit/tree/69cb5195f414ceb6398c5581254673d2c6c8d0d8), approximately 150 MB total, with Apache-2.0 model metadata; recheck exact bytes through the admin acquisition pipeline during implementation. Production selection remains capability-based.

## Clarification outcome

The 2026-08-29 answers remain in force. Two questions were asked and answered on 2026-09-28: text/code/PDF/image uploads, and both text and scanned PDFs. Both are recorded in spec.md. Other defaults remain one active turn, explicit retry, fresh coding snapshots, blocked plain-chat overflow, owner-only data and desktop acceptance. No unresolved technical choice blocks planning.

## Scope and verification boundaries

No engine, deployment, model acquisition, dependency installation or production mutation is performed in this planning stage. Research uses current local code and the primary references above. Implementation must add contract tests for every new REST/event/node shape, unit and web tests, local tiny-model integration, browser acceptance, image policy/CVE/SBOM checks, observability and an operational runbook.
