# Web interface operations

The local interface uses the existing authenticated HTTPS address on Core. Feature 025 revises React/CSS/font presentation and fixes late image-admission refusal
auditing. It adds no API shape, database migration, external runtime request or network listener. The reference theme is light ivory/navy/gold with an explicit dark
option. The theme preference `coire.theme` is non-sensitive and independent of private drafts.

## Inspect and validate

Run `pnpm -C apps/coire-web test`, `pnpm -C apps/coire-web lint` and
`pnpm -C apps/coire-web build`. The build includes strict TypeScript. Format touched frontend
files with the project's Prettier configuration. Shared API declarations remain generated.
Self-hosted Inter and Cormorant Garamond fonts include OFL1.1 notices and source hashes under
`apps/coire-web/public/fonts`; never replace them with runtime Google Fonts requests.

Check sign-in, main routes, active sidebar state, admin permissions, capability-gated
Upgrades, model selection, draft suggestions, history and mobile navigation. Theme changes
must survive reload while blocked storage must not prevent use. Inspect desktop light/dark,
1024px tablet and 390px phone screenshots. Verify long code and tables scroll internally,
keyboard focus remains visible, and the mobile sign-out icon retains its accessible label.

The overview counts active instances separately from historical stopped/failed records. Read
historical details in Instances. Unknown cluster status is Connecting, never green by default.
Existing API spans/metrics/audit continue to cover the same operations; no new backend path or
telemetry destination is introduced by the visual redesign.

## Deploy and roll back

Build the dedicated web image with `apps/coire-web/Dockerfile`, run all image-policy rules,
a CRITICAL scan including unfixed findings, and produce its SPDX SBOM. Preserve the current
web image ID and installed Compose file before changing anything. Use the existing private
local sign-in deployment at `~/.coire/projects/coire/local-signin/current/compose.json`; retain
its HTTPS configuration, Keychain-derived certificate mounts, ports, networks and hardening.
Replace only the web image with the validated digest and recreate only `coire-web` with
`--no-deps`. Do not rebuild/recreate API, scheduler, nodes or observability for a visual change.

Check actual owner sign-in, all visible routes, a live admin snapshot, light/dark preference,
mobile navigation and logout/replay refusal. HTTP browser entry must continue redirecting to
HTTPS while the original HTTP machine health route stays available. To roll back, restore the
recorded prior web image ID in that same installed Compose file and recreate only web.
API/database state, credentials and conversations require no rollback for this feature.

Keep browser screenshot fixtures separate from production data. Stop the owned loopback
preview server, remove temporary browser fixtures, and verify no test containers/volumes
remain before reporting completion. Never record credentials in screenshots, Git or logs.

## Explicit image access

In Admin → Users & keys, grant **explicit** to the intended active human account. The wire
entitlement is exactly `explicit`, not `explicit_image`; browser grants/revocations use the
existing audited admin route. The current identity refreshes after a console change.
In Images choose **Content mode → Explicit · anatomy & figure studies**, enter a prompt
and Generate. Human anatomy and nonsexual figure studies are supported; outputs remain
private and carry their recorded explicit tag. Existing service-token refusal and personal
key `images` + `images:explicit` requirements remain enforced by the backend. Do not
change scopes, classifier or entitlement checks to make a test pass.

Acceptance uses a nonsexual adult fine-art anatomy prompt at the published model's actual
512px/four-step limits, then checks successful job, explicit recipe/tag, owner download grant,
PNG SHA256 and grant/submit/dispatch/completion audit rows. No fixed model guarantee beyond
the installed registry profile is implied. Keep the qualification output outside Git.

Use headless Playwright WebKit for owner-directed visual acceptance. Verify the installed
local certificate/hostname using the exact CA file before a browser-context-only TLS test
exception; never change OS trust or widen production networking. Capture settled transitions,
local fonts, desktop/dark/phone and long-content layouts and inspect the actual screenshots.

## Late image-admission refusal auditing

Issue102 fixes a refusal occurring after the route guard: admission's ImageForbidden now
rolls back provisional job/quota state and releases row locks before committing the existing
independent `image.refused` audit. Inspect action `image.refused`, target `POST /api/v1/images`,
actor/key identity and fixed authorization reason; prompts and credentials must be absent.
The guard's early refusals continue using their existing audit path. No permission is relaxed.

This small backend fix requires the separately validated API image. Save the current API
image ID and compose file, replace only `services.coire-api.image`, and recreate only API
with `--no-deps`. No migration is needed. The qualification snapshots under
`~/.coire/qualification/025/audit-activation` retain the prior API image for API-only rollback.
Do not restore a whole older compose file if a newer web image must remain installed.

## Literal reference theme correction

The owner rejected the previous sidebar/card composition. The current visual authority is
the supplied Andrei Nita page itself; `styles/reference.css` applies its horizontal masthead,
centered672px column, measured32/40 headings,16/25.6 prose, quiet rules and actual brown/gold
actions. Admin tables can use the reference's1152px breakout. History is now an explicit
Conversations control at desktop as well as mobile; Escape restores its toggle focus. New
visitors follow system theme unless an explicit preference exists. Check sign-in wordmark
visibility, mobile masthead and gallery action wrapping in actual WebKit screenshots.

For this correction replace **only web**. Retain the installed API audit fix and the owner's
explicit entitlement. Use a new current-image/config snapshot; never apply a whole earlier
rollback manifest that would unintentionally revert the API fix. Evidence under
`~/.coire/qualification/025/literal-*` includes matching-viewport captures and measured parity.

## Sidebar restoration and color themes (current)

The owner reverted the literal-reference layout. Restore the1f026b60 sidebar presentation;
`styles/reference.css` is removed. Palette controls now live beside the light/dark toggle on
workspace and sign-in. Choose Original/Ocean/Forest/Plum, or Create custom theme to edit
light/dark page, panel, sidebar and accent colors. Live preview is not saved until Save theme;
Cancel/Escape discards it. Edit/delete apply to custom themes; Reset colors selects Original
without deleting saved palettes. Preference names must be unique and1–40 characters.

Themes are browser-local (`coire.color-themes.v1`,version1,maximum12 custom themes) and do
not sync between devices/accounts. The existing `coire.theme` key still controls light/dark.
If storage is blocked, colors continue to work for the current visit and the dialog explains
that saving failed. To recover corrupt preferences, use Reset colors or remove only the
color-theme key from this browser; do not clear all storage/private chat drafts.

Validation includes hex-only input, compatible page/panel text contrast and bounded stored
data. Foreground/semantic label colors are derived; no arbitrary CSS, remote resources,
admin changes or backend contract are involved. Check preset persistence across sign-in
and reload, custom save/edit/cancel/delete/reset, keyboard Tab/Shift+Tab/Escape and phone
dialog scrolling in headless Playwright WebKit. Color choices must not move the sidebar or
change existing controls/data. No model workload is required for these presentation changes.

Only web is rebuilt/deployed; retain API audit-fix image73a0fd0 and existing explicit-image
entitlement/controls. Snapshot current web/compose before --no-deps recreation. For rollback
use web135c7a20 (the restored layout without palette editor), preserving the API image.
Private current receipts/screenshots are under `~/.coire/qualification/025/palettes-*`.

### Image submission feedback (issue #103)

The generation form acknowledges submission immediately beneath Generate. Accepted jobs
show queue position when supplied, preparation, generation steps when measured, saving,
and a terminal ready/failed/stopped state. Job identifiers and cache/worker diagnostics
are under **Job details**. Stop remains available until the server confirms a terminal
state. A dropped event connection is labelled as reconnecting; it never implies that
an accepted job failed. **View in library** clears the tag filter and moves keyboard
focus to the library. Reloading Images resumes observation of an active job from the
owner's job list; **View progress** selects another listed job without resubmitting it.

This uses the existing authenticated image SSE/API contracts and observability. No
percentage, queue position or completion time is invented. There is no engine or API
change. Roll back by restoring the previous web image in the installed compose manifest
and recreating only `coire-web` with `--no-deps`; preserve the API and all data volumes.

### Populated and interrupted states (issue #105)

Image cards reserve enough width for whole action labels and fit their content.
Reuse settings and Download stay visible; More actions discloses regeneration,
new-seed generation and confirmed deletion. Generation history is collapsed by
default, shows five spaced job summaries when opened, and offers Show older jobs.
The generator and history use the page scroll rather than nested scroll panels.

An empty assistant response renders no blank bubble or answer-feedback form. A
failed/interrupted turn without answer text says No answer received; a partial answer
says Response interrupted and retains its continuation control. Completed answers
have compact, labelled rating buttons; optional feedback tags are a disclosure.
These changes affect presentation only. They do not change stored answers, retry
semantics, feedback capture preferences, authorization or engine failure behavior.

Visual acceptance must inspect populated cards, expanded actions/history, empty and
partial interrupted turns, and a completed answer with feedback and a code block.
Use headless Playwright WebKit in both appearances across desktop and mobile; check
word wrapping, element bounds/overlaps and nested scroll regions as well as page
overflow. Screenshot fixtures must be labelled separately from live API evidence.

### Unified chat composer

Use the paperclip in the composer to attach one file, or drop/paste a file into it.
A chip shows processing or inclusion in the next message; its × removes inclusion,
not the stored conversation file. Manage files inside the composer retains explicit
file selection, PDF text/page-image settings, preparation retries and downloads.
Uploads become visibly selected, including while processing, and Send remains gated
on readiness, context limits and the selected model's capabilities. The upload limit
and private authenticated storage path are unchanged.

Enter sends and Shift+Enter inserts a line; IME input is preserved. The circular Send
action becomes Stop after the server accepts a response. The draft stays editable
while generation runs. A long thread keeps the composer available; scrolling back
preserves the reading position and exposes Jump to latest. These are web changes,
with existing chat/file APIs, scopes, audit and telemetry. Roll back the web image
with --no-deps; do not replace API services or delete conversation volumes.
