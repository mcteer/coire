# Image workspace reference inspection

Inspected 2026-10-16. Official documentation was opened in headless Playwright WebKit
at 1440×1000. Midjourney's embedded interface screenshots were downloaded and viewed
at their native resolution. These are documented product interfaces, not a claim of
having signed into those products. Evidence is private under
`~/.coire/qualification/026/references/`, including results.json and screenshots.

| Source | Observed pattern | Coire decision |
| --- | --- | --- |
| [Midjourney creation](https://docs.midjourney.com/hc/en-us/articles/33390732264589-Creating-on-Web) | Compact top prompt bar; central image feed; thumbnail hover tools; click-through image viewer | Compact prompt workbench, image-first gallery, overlay actions and large preview |
| [Midjourney organizing](https://docs.midjourney.com/hc/en-us/articles/33329462451469-Organizing-Your-Creations) | Selection count and contextual bulk-action bar | Explicit selected count, clear selection, confirmed bulk deletion |
| [Firefly aspect ratios](https://helpx.adobe.com/firefly/web/work-with-images/generate-images/set-aspect-ratio-of-images.html) | Named landscape/portrait/square proportions and visual examples | Named ratio choices with an aspect preview and actual output pixel dimensions |

Retain Coire's existing themes and navigation. Use plain-language workflow labels and
show the required image inputs directly for the selected workflow. Keep secondary
engine settings in Advanced. Do not copy Midjourney trash semantics: Coire's requested
permanent deletion must have explicit confirmation and actual backend deletion.

Review checklist: prompt-to-progress continuity; readable mode-specific inputs; image
preview and focus restoration; thumbnail hover/focus/touch actions; bulk selection and
failure state; history deletion confirmation; resolution truthfulness; no narrow-screen
overflow. Inspect Coire itself after implementation, not only reference screenshots.
