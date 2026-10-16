# Coire interface — reference theme

The visual authority is the owner's exact page:
https://andreinita.co/learning/pydantic-ai/ . The owner rejected the earlier sidebar,
card-heavy workspace and marketing welcome. Those interpretations are superseded.
Do not introduce another dashboard product's visual system as an alternative authority.

## Measured styling

Headless Playwright WebKit,1440×1000, identical light/dark viewports:

| Property | Reference | Coire |
|---|---|---|
| Masthead | 64px, horizontal links, text wordmark | Same; role-aware application links |
| Wordmark left | 144px | 143.984px WebKit subpixel rounding |
| Editorial column | 672px, centered, left384px | Same |
| Page heading | Cormorant Garamond500,32px/40px | Same |
| Heading origin | x384,y214 | Same |
| Primary prose | Inter400,16px/25.6px | Same |
| Supporting prose | Inter400,14px/22.4px | Same |
| Light background/ink | #faf8f5 / #040810 | Same |
| Dark background/ink | #081027 / #e8edf8 | Same |
| Action fill/text | #6b4e1a / #e6cb9a | Same |

Use the reference's open sections, fine rules, subdued surfaces and subtle gold radial wash.
Primary actions follow the measured source-banner button; secondary actions remain quiet.
No desktop sidebar, giant welcome headline, floating dock, custom emblem or split sign-in hero.
`apps/coire-web/src/styles/reference.css` is the canonical applied theme, loaded after feature
layout styles. `tokens.css` defines the shared palette/fonts. `docs/design/tokens.css` mirrors
runtime tokens; the applied stylesheet resolves feature-specific legacy presentation.

## Functional adaptation

Chat and sign-in use the measured centered column and hierarchy. Chat history is an explicit
Conversations disclosure at every width; Escape closes it and restores focus. Suggested drafts
remain simple numbered rows and only prefill. Actual model selection, file/code/feedback,
streaming, cancellation and authentication behavior remain unchanged.

Administrative headings use the same column, with data tables/node summaries in the site's
1152px breakout where required. Admin subnavigation is a horizontal text rail on desktop;
mobile uses the existing keyboard-accessible drawer. Image generation and library use open
ruled sections with actual image/entitlement controls. Training keeps its working controls.
Dense data remains real and owner/admin scoped. Unknown/stale/alert states never imply health.

New visitors follow their system color preference, as the reference does; an explicit saved
light/dark choice takes precedence. Preference storage is guarded and separate from private
drafts. Fonts remain self-hosted Inter/Cormorant Garamond with existing OFL notices and hashes.

## Acceptance

Use **headless Playwright WebKit**, inspect both the reference and actual application screenshots
at matching viewport sizes, and compare measured styles. Check1440px desktop,1024px tablet,
390px phone, both themes, sign-in, chat/history/long code, admin, Images and Training. Passing
functional tests or token similarity alone is not visual acceptance. Do not record owner approval
without owner feedback. Credentials, screenshots and generated outputs stay outside Git.
