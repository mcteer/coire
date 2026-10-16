# Coire interface — sidebar workspace and color themes

Feature [025](../../specs/025-professional-interface/spec.md) supersedes the historical Glass
mockups and their floating dock. This document and `tokens.css` are the current visual source
of truth; the architecture and typed contracts remain authoritative for behavior.

## Current owner decision

The owner requested restoration of the sidebar layout after rejecting the literal website
layout, and explicitly chose **both presets and custom color themes**. The restored layout
is the1f026b60 presentation. Theme customization changes colors only; navigation, spacing,
typography, operational data and generation/authentication behavior remain fixed. The
literal masthead/column stylesheet is removed and is no longer the visual authority.

## Direction and references

The owner's [Andrei Nita reference](https://andreinita.co/learning/pydantic-ai/) supplies warm
ivory, deep navy, muted gold, Inter body type and Cormorant Garamond editorial headings.
[Linear's redesign study](https://linear.app/now/how-we-redesigned-the-linear-ui) informs
navigation hierarchy, alignment and restrained density. The official
[Vercel chatbot](https://github.com/vercel/ai-chatbot) informs a focused conversation/composer
layout. No reference content or branded asset is copied into Coire.

## Visual system

- Default light: ivory `#faf8f5`, paper `#f2eee8` raised surfaces, near-black `#040810` text and visible warm `#c2b8a4` hairlines.
- Navigation: tonal paper in light mode and navy in dark mode, readable labels and a gold active rule. It anchors
  the workspace rather than floating above content.
- Gold is reserved for primary actions, selected states and small brand accents. Repeated
  table actions use quieter outlined buttons; dangerous actions retain red labels.
- Dark mode uses navy surfaces, light ivory text and softened gold. Explicit user preference
  is stored as `coire.theme`; storage failures must never block sign-in or navigation.
- Inter is the UI/body face. Cormorant Garamond is limited to page titles, sign-in and the
  conversational welcome. Operational numbers and code use system monospace. Body text is 16px, controls 14px and editorial headings 32–42px. Both font
  assets are self-hosted; sources, SHA256 hashes and full OFL1.1 notices live in `public/fonts`.
- Corners are restrained (7–12px), dividers are subtle, and shadows only establish surface
  hierarchy. Do not restore translucent cards, bright gradients or a floating bottom dock.

## Shell and navigation

Desktop uses a 240px sidebar and a 64px top header. The header contains breadcrumbs, theme
control, sign-out and truthful cluster state on Admin. Unknown state says Connecting; never
infer health from the absence of a snapshot. Primary routes retain role checks. Admin subviews
sit in the sidebar and Upgrades remains capability-gated. Content is aligned to a shared grid.

Below 760px the sidebar becomes a labelled drawer, opened through a real button. Escape
closes it, focus returns to the toggle, and Tab stays inside while open. Primary and admin
navigation close the drawer after selection. Icons without visible text require accessible
names. A skip link reaches the main workspace content.

## Conversation

History shares the desktop navigation column. On phones it is an explicit Conversations
control. Model selection is a native disclosure showing the selected model and provider;
all exact-target and capability checks remain. The selected provider must remain visible.

The empty conversation uses a readable editorial heading and three suggested drafts. These
only fill and focus the composer: they never submit, switch model, or enable permissions.
Chat/Code, attachments and feedback privacy remain real controls. Secondary settings live in
labelled disclosures rather than occupying the entire first screen.

Messages use a restrained user surface and an open assistant surface. Markdown, code-copy,
reasoning disclosure, attribution and file selections keep their existing semantics. Long
code scrolls within its block, never widening the page. The composer preserves Enter/Shift+
Enter, IME handling, disabled states, progress and server cancellation behavior.

## Dashboards, images and training

The overview leads with actual online Studios, available placement memory and active
instances. Stopped/failed instance history belongs in Instances; it must not inflate the live
count or produce an endlessly scrolling overview. Every number comes from the current typed
snapshot. Node cards retain actual resource/heartbeat/health data. Long tables scroll inside
their panel with a sticky header.

On wide screens Images places generation controls beside the library; jobs and presets stay
reachable. Training keeps runs, the working panel and supporting details responsive. All
forms, empty/loading/error states, confirmation dialogs and destructive actions share the
same tokens. Do not add fake trend charts, counts, time-series or decorative controls.

## Accessibility and acceptance

Maintain 4.5:1 normal-text contrast, visible focus, semantic labels, textual status and
reduced-motion support. Verify real 1440px, 1024px and 390px browser layouts in light/dark.
Inspect sign-in, empty and filled chat, long code/identifiers, model selection, admin tables,
Images and Training. Screenshots are private execution artifacts outside Git. Presentation
work must not weaken authentication, API contracts, entitlement checks or safety gates.

## WebKit acceptance and explicit images

Owner correction requires actual **headless Playwright WebKit** visual inspection, including
the supplied reference, sign-in, empty/long chat, dashboard tables, Images/Training, desktop,
phone and dark mode. Inspect rendered screenshots, not only dimensions or a passing build.
Native selects use a consistent code-native chevron and minimum42px height in WebKit.
Images leads with model, prompt, content mode and Generate before secondary settings.
Standard/Explicit uses the existing generated content_mode contract. Explicit selection
requires the current user's canonical `explicit` entitlement; an explicit preset cannot be
downgraded by the form, and restored explicit settings are blocked without current access.
The admin console grants/revokes `explicit` through the audited existing API and refreshes
the current identity. Backend admission remains authoritative at every action boundary.

## Color themes

The header palette button opens **Color themes** on both sign-in and the workspace. Original
retains the restored CSS exactly by clearing palette overrides; Ocean, Forest and Plum offer
coordinated alternatives. Every palette has independent light/dark page, panel, sidebar and
accent colors, while the existing appearance toggle remains available.

Users can create, preview, save, edit and delete up to12 named custom palettes. Native color
inputs and editable six-digit hex fields support both ways of choosing colors. Preview is
transient; Cancel/Escape restores the selected saved palette and returns focus. Reset colors
selects Original without deleting custom palettes. The dialog traps Tab/Shift+Tab and fits
phone screens with internal scrolling. Custom colors never inject arbitrary CSS or URLs.

`theme/colorThemes.ts` contains bounded browser-local preference validation and contrast
math; `useTheme` owns appearance, selected palette, transient preview and persistence. Store
only versioned color preferences at `coire.color-themes.v1`; keep `coire.theme` for the existing
light/dark choice. Never use these keys for credentials, prompts, registry state or entitlement.
Preferences are local to this browser and disclosed as such; blocked storage remains usable
with an explicit persistence message. Malformed/stale preference data has a safe fallback.

Text, accent labels, sidebar labels and button text are derived for at least4.5:1 contrast
against their generated surfaces. Reject incompatible page/panel combinations rather than
allow unreadable text. Existing semantic health/danger labels keep their meanings. This is
a palette text-contrast target, not a claim of complete accessibility certification.
