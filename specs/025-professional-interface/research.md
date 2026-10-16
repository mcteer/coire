# Design research

Owner reference: https://andreinita.co/learning/pydantic-ai/ inspected in a real 1440px browser. Computed default body is ivory rgb(250,248,245), Inter text and Cormorant Garamond headings; the source defines navy surfaces and gold accents for its alternate theme. Carry over these visual qualities, not the blog layout or content.

Professional structure: https://linear.app/now/how-we-redesigned-the-linear-ui emphasizes consistent sidebar/header alignment, reduced visual noise and clear content hierarchy; https://vercel.com/design provides restrained system/surface treatment. Chat research includes the official Vercel AI Chatbot repository and preview (https://github.com/vercel/ai-chatbot); the old chat.vercel.ai demo returned DEPLOYMENT_NOT_FOUND, so it is not claimed as a live app walkthrough. Existing ChatGPT/Claude conversation conventions also inform: persistent history, centered readable content and stable composer. These inform layout rather than copied assets.

Fonts: Google Fonts Inter and Cormorant Garamond are licensed SIL OFL1.1, confirmed at https://github.com/google/fonts/blob/main/ofl/inter/OFL.txt and https://github.com/google/fonts/blob/main/ofl/cormorantgaramond/OFL.txt. Bundle unmodified Latin WOFF2 assets, full notices and source/hash inventory. No runtime font/CDN requests.

Decisions: default light reference theme; explicit persisted dark mode; navy navigation shell; gold limited to active/primary affordances; serif only for editorial page/empty-state headings, sans-serif for forms/data. Keep existing truthful data and permission controls. Historical Glass mockups are superseded by feature025 and updated canonical design documentation. Mobile drawer restores toggle focus and closes with Escape or navigation. Theme failures never block authentication.

## Owner correction and WebKit comparison
The owner identified composition and typography/color/spacing as the mismatches. Revisited
the actual source in headless Playwright WebKit26.0 (build2215). Its light navigation and
surfaces are paper100 `#f2eee8`, overlay `#e7e1d8`, border `#c2b8a4`, primary ink `#040810`,
secondary ink `#313762` and text-link gold `#6b4e1a`; the first pass's dark sidebar/white cards
did not carry this through. Revised those exact core colors and dark navy800/900 values,
14–16px reading/control sizes and 32–42px editorial scale. Retained accessible contrast
for muted text rather than copying low-contrast reference placeholder values.

Playwright's Node26 download extractor stalled; the official downloaded WebKit archive was
extracted with Python preserving symlinks/modes, then successfully launched headless through
Playwright. No application package/dependency was added. WebKit native selects require
explicit appearance/size/chevron styling for the intended control treatment. Real browser
screenshots—not Chromium-only or overflow checks—were inspected in both themes.

Issue #101 tracks the image form/entitlement mismatch. Image API supports content_mode
already; no wire-schema or backend change is needed. Existing canonical entitlement is
`explicit`. Real owner account initially lacked it; unauthorized admission403 was measured,
then the authenticated audited admin grant enabled a successful Studio anatomical image
and private verified download. Issue #102 records an existing late-refusal audit gap discovered during that negative
probe. The implementation scope was amended to close it: rollback request locks before the
existing independent refusal helper. Live restricted-key403 now has a matching audit row;
authorization remains enforced, and successful operations/grants have audit evidence.

## Literal styling authority after second rejection
The owner explicitly rejects the previous result. Root cause: we substituted a generic
sidebar/dashboard shell and focused on palette tokens rather than copying the page's visual
structure and actual component styling. The reference is now the sole visual authority.
Loaded its scripts/fonts in actual headless WebKit and measured both themes at1440×1000:
672px column,left384px,h1 origin214px, Cormorant50032/40, Inter16/25.6, support14/22.4,
64px horizontal masthead, source CTA #6b4e1a/#e6cb9a. Prior inverse gold buttons and marketing
hero were inventions. Apply the open ruled composition, numbered rows, subtle radial wash,
text wordmark and reference button treatment. Business data/controls remain Coire's.

Preview measured those column/heading/header properties directly in both themes. Brand
left143.984px differs by1/64 CSS pixel from nominal144; use0.1px tolerance only for that
WebKit subpixel rounding. Actual screenshots are inspected separately from these assertions.
