# Coire web UI — design specification ("Glass")

This document, `tokens.css`, and the five files in `mockups/` are the source of truth for `apps/coire-web`. When code and mockup disagree, the mockup wins on look and the architecture doc wins on behaviour. Build components from the tokens; never hard-code a colour, radius, or shadow that already has a token.

The mockups are static 1440×900 frames with sample data. Their CSS is intentionally flat and inline so it is easy to read; the app should reimplement it as React components with the same measurements, not copy the files wholesale. Model names, numbers, and conversations in the mockups are placeholders.

## 1. Principles

1. **Open stage, floating panels.** Every page is a soft ground with a few translucent panels on it. Content should never fill the viewport edge to edge; the ground stays visible around and between panels.
2. **One dense page.** Admin is allowed a tile grid. Every other page keeps one primary panel plus at most two side panels.
3. **Text carries meaning; colour confirms it.** Status is always a word plus a colour, never colour alone. Identifiers, numbers, and machine names use the mono face so they read as data.
4. **Monospace is for data, not decoration.** Registry ids, memory figures, timestamps, token rates, seeds, and code use `--font-mono`; everything a person reads as prose uses `--font-ui`.
5. **Show the machinery only where it helps.** Cluster state appears as a small widget or chip, and expands only on Admin. Warm-up and queue positions are surfaced because they change what the user should expect, not to look technical.

## 2. Shell (identical on every page)

- Ground: `--bg` with two radial tints, `radial-gradient(1100px 600px at 15% 0%, var(--bg-tint-a) 0%, transparent 60%)` and `radial-gradient(900px 600px at 100% 100%, var(--bg-tint-b) 0%, transparent 60%)`. Fixed to the viewport; it does not scroll with content.
- **Brand + breadcrumb**, top-left at `--shell-margin` / `--shell-header-top`: 34px gradient tile with the cauldron mark, "Coire" 17px 700, then `/ Section / Current item` where only the current item is `--ink` and bold.
- **Status chips + avatar**, top-right: pill chips (`--r-pill`, glass fill, 12px 600) for cluster health and any instance currently warming, then a 32px `--ink` avatar. Chips are informational; clicking one opens Admin › Instances.
- **Dock**, bottom-centre at `--shell-dock-bottom`: a glass pill containing five items (Chat, Training, Images, Settings · divider · Admin). Items are 82px wide, icon 22px above an 11px 700 label. The active item is a white card with `--shadow-1` and an accent-coloured icon. Admin is hidden for non-admin users; the divider goes with it.
- **Sub-tab bar** (Settings, Admin): a glass segmented control centred horizontally at `top: 84px`; content then starts at `150px`. Active tab is a white card with `--shadow-1`.
- Pages are laid out on a 1440px reference; the app is desktop-first. Below 1200px the side panels collapse to drawers behind buttons in the breadcrumb row; the dock stays.

## 3. Surfaces and elevation

| Surface | Use | Recipe |
|---|---|---|
| Glass panel | every floating panel (history, widget, main panel, dock, tab bar) | `--glass-bg`, 1px `--glass-border`, `backdrop-filter: blur(var(--glass-blur))`, `--r-2xl`, `--shadow-2` |
| Solid card | rows and cards inside a glass panel | `--surface`, 1px `--border`, `--r-lg` |
| Inset | code blocks, icon buttons, picker chips, inputs | `--surface-2` (or `--surface-3` for inputs), no border unless it is an input (1px `--border`) |
| Dark | user chat bubble, code in Settings, Ask-Coire prompt | `--ink` fill, white text, `--shadow-dark` |
| Composer / popover | chat composer, model picker, menus | `--surface`, `--r-3xl`/`--r-xl`, `--shadow-3` |

Never nest glass inside glass. A glass panel contains solid cards; a solid card contains insets.

## 4. Typography

Manrope for UI (400 body, 500 secondary, 600 labels and nav, 700 titles and chips, 800 page headings). DM Mono 400/500 for data. Sizes are the `--text-*` tokens; page section headings are 20px/800 with −0.01em tracking; panel titles are 12px/700 `--ink-2`; table headers and field labels are 11px/700 uppercase with 0.04–0.06em tracking in `--ink-3`. Chat messages are 15px at 1.55 line-height. Load both faces from Google Fonts with `display=swap` and give them the fallback stacks in `tokens.css`.

## 5. Components

**Buttons.** Primary: `--accent-gradient`, white 13px/700, `--r-md`, `--shadow-accent`, 8px 14px padding. Ghost: `--surface`, 1px `--border`, `--ink`. Danger: ghost with `--bad-ink` text and a `#f1c9c6` border; used for Stop/Kill/Cancel. Icon buttons are 34px squares (`--r-md`, `--surface-2`) with 16px stroke icons.

**Status pill** (`.st`): `--r-pill`, 11px/700, 3px 8px padding. Variants ok / warm / bad / dim / ind map to the `--*-soft` fill and `--*-ink` text tokens. Vocabulary is fixed: `ready`, `loaded`, `warming NN%`, `cold`, `queued`, `running`, `done`, `failed`, `verified`, `apply`, `published`, `admin only`, `hidden`, `granted`.

**Status dot**: 8px circle with a 3px soft ring of the same hue; `cold` has no ring.

**Progress ring** (`.ring`): 7px track `--divider`, fill `--accent-gradient-h`, both `--r-pill`. Ledger bars on Admin use segmented fills with a 2px gap and a legend beneath.

**Model picker chip**: inset chip in the composer showing dot + display name + muted variant/node, chevron. Clicking opens the picker popover: grouped by task (Coding, General, Reasoning, Images), each row = dot, name, one-line description with variant · node · load state, and a status pill. Cold models show an estimated warm-up time. Models the user is not entitled to are not listed.

**Chat bubbles**: user = dark, right-aligned, 18px radius with a 6px bottom-right corner, max 640px. Assistant = translucent white (0.82) with blur, left-aligned, 6px bottom-left corner, max 700px, meta row (dot, model, mono timing) above the text. Thinking blocks are a 2px left-border inset in `--ink-3`, collapsed by default when the user has enabled them. Code blocks are `--surface-2`, 12px radius, 12.5px mono. Feedback row: thumbs up, thumbs down, regenerate, "Compare", copy — 28px inset buttons.

**Composer**: 760px, `--surface`, `--r-3xl`, `--shadow-3`; a 40px minimum text area, then a row with the model picker chip, attach and format icon buttons, slash-command hint in mono, and a 38px gradient send button.

**Tables** (Admin, Settings › API keys): 13px, header row 11px uppercase `--ink-3` with a `--border` rule, cells 9px 10px with `--divider` rules, no zebra striping. Actions are text links at the row end.

**Forms**: field label 11px uppercase, input 9px 12px on `--surface`/`--surface-3` with `--border`, `--r-md`. Sliders are 6px tracks with a 14px white knob ringed in `--accent`. Toggles are 34×20 pills.

**Event timeline** (image jobs, run logs): 8px dots (done = ok, current = accent with ring, next = `--border`), 64px event label, then detail in mono.

**Charts**: single-hue line charts in `--accent`, 2px stroke, recessive `--divider` gridlines, mono axis labels in `--ink-3`, no legend for one series; a secondary dashed line in `--accent-2` with 4px white-filled markers for eval points. Always a hover tooltip; the mock shows the final value as a direct label.

## 6. Page anatomy

**Chat** (`mockups/chat.html`): left history panel (236px) with "Recent", conversations as name + muted subtitle, and a dashed "+ New conversation"; centre stage 760px for messages; right widget (264px) with cluster bars and "This conversation" facts (context used, tools loaded, verified state); composer bottom-centre 112px above the viewport bottom so the dock never overlaps it. Cold-model warning and queue position appear as a line inside the composer, not as a modal.

**Training** (`mockups/training.html`): left runs list (276px) with status pills and a primary "+ New run from recipe" pinned to the bottom; centre run panel (800px): title row with status and Pause/Stop, four spec cards (base model, objective · parameterization, data, optim), progress bar with step/loss/ETA and the ledger reservation, loss chart with checkpoint chips, eval-vs-base table, log tail, and the stored `TrainingSpec` YAML in three mono columns; right panel (264px) with datasets (type, rows, provenance, analyze stats, mixture membership) and adapters.

**Images** (`mockups/images.html`): left presets (236px) with a swatch per preset and the entitlement pill on explicit presets, then mode chips; centre form (400px) for the `ImageSpec` (prompt, negative, size/steps/guidance, seed/count/format, LoRA stack sliders, Generate + Save preset); right output panel (704px) with the current job (preview thumbnail + event timeline), results grid (4 columns, square, caption with seed/steps, hover actions regenerate and reuse, `explicit` tag when applicable), and three worker facts (worker residency, queue, cache).

**Settings** (`mockups/settings.html`): sub-tabs Account · API keys · Connect tools · Feedback & data · Usage; a single centred 760px panel showing one tab. Account shows profile, explicit entitlement, sessions, then Defaults (per-task model, thinking toggle). API keys is a table (name, prefix, scopes, budget, last used, rotate/revoke). Connect tools shows the OpenAI, Anthropic, and MCP base URLs with copy. Feedback & data holds the training-consent and history toggles and export. Usage shows month-to-date counters.

**Admin** (`mockups/admin.html`): sub-tabs Overview · Models · Instances · Runs & jobs · Users & keys · Upgrades · Audit. Overview is a 3-column grid: three node tiles (studio-a, studio-b, core + link) with segmented ledger bars and legends; a wide Runs & jobs tile (icon, name, detail, kill/stop/cancel or status) with Alerts beneath; and Ask Coire, the ops agent, whose proposed actions render as Confirm buttons that write an audit row. Models tab is the roster table (model, variants, state, placement, visibility toggle, verified, idle TTL, actions) with "+ Add from Hugging Face" as the only entry point for acquisition.

## 7. Interaction rules

- Loading is never blocking. Cold-model selection shows the warm-up estimate inline and streams keep-alive until the first token; a queue position replaces the estimate when the scheduler queues the request.
- Destructive actions (Kill, Stop, Cancel, Retire, Revoke) are red text or danger buttons and confirm inline (the button becomes "Confirm?" for 4 s), not with a modal. Ops-agent proposals always require an explicit Confirm click.
- Streaming text appends; earlier content never reflows. Thinking blocks stream into a collapsed summary line.
- Hover on any mono figure (memory, tokens, latency) shows the exact value and source in a tooltip.
- Keyboard: `⌘K` command palette (jump to a model, conversation, run; admin actions when admin), `⌘1–5` dock items, `⌘↵` send, `Esc` closes popovers.
- Motion is limited to `--t-fast` fades for popovers and `--t-med` width transitions on progress bars. No page transitions.

## 8. Accessibility

Contrast: all text on glass panels meets 4.5:1 (`--ink-2` on white ≥ 7:1, `--ink-3` is reserved for ≥ 12px secondary text and meets 4.5:1 on `--surface`). Focus rings are 2px `--accent` outlines with 2px offset on every interactive element. Status pills carry text; icons carry `aria-label`s. Tables are real `<table>` elements; the dock is a `<nav>` with `aria-current`. Reduced-motion preference disables the blur transition and progress animation.

## 9. Out of scope for v1

Dark theme (tokens are structured so a `[data-theme="dark"]` block can redefine surfaces, inks, and shadows; the accent pair stays), mobile layouts, and the Models/Instances/Users/Upgrades/Audit admin tabs beyond the table pattern described above — build them from the Admin overview's tiles and the table spec.
