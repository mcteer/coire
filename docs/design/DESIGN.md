# Coire interface — professional workspace

Feature [025](../../specs/025-professional-interface/spec.md) supersedes the historical Glass
mockups and their floating dock. This document and `tokens.css` are the current visual source
of truth; the architecture and typed contracts remain authoritative for behavior.

## Direction and references

The owner's [Andrei Nita reference](https://andreinita.co/learning/pydantic-ai/) supplies warm
ivory, deep navy, muted gold, Inter body type and Cormorant Garamond editorial headings.
[Linear's redesign study](https://linear.app/now/how-we-redesigned-the-linear-ui) informs
navigation hierarchy, alignment and restrained density. The official
[Vercel chatbot](https://github.com/vercel/ai-chatbot) informs a focused conversation/composer
layout. No reference content or branded asset is copied into Coire.

## Visual system

- Default light: ivory `#faf8f5`, white raised surfaces, navy `#152033` text, quiet warm borders.
- Navigation: navy `#101a2c`, readable muted labels and a gold active indicator. It anchors
  the workspace rather than floating above content.
- Gold is reserved for primary actions, selected states and small brand accents. Repeated
  table actions use quieter outlined buttons; dangerous actions retain red labels.
- Dark mode uses navy surfaces, light ivory text and softened gold. Explicit user preference
  is stored as `coire.theme`; storage failures must never block sign-in or navigation.
- Inter is the UI/body face. Cormorant Garamond is limited to page titles, sign-in and the
  conversational welcome. Operational numbers and code use system monospace. Both font
  assets are self-hosted; sources, SHA256 hashes and full OFL1.1 notices live in `public/fonts`.
- Corners are restrained (7–12px), dividers are subtle, and shadows only establish surface
  hierarchy. Do not restore translucent cards, bright gradients or a floating bottom dock.

## Shell and navigation

Desktop uses a 232px sidebar and a 68px top header. The header contains breadcrumbs, theme
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
