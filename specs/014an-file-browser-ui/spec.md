# Feature Specification: Browser File Preparation

**Feature Branch**: `feat/014an-file-browser-ui`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AN1: The Chat browser uploads one supported file at a time with owner cookies, creates a conversation first when necessary, and reconciles the server's revision and attachment list after admission.
- FR-AN2: The owner can see processing, failure and ready states; explicitly retry inspection; download originals; and view only owner-scoped private PNG previews. The browser never embeds an original PDF or puts a credential in a URL.
- FR-AN3: A ready PDF exposes its full page count, an explicit text or visual mode and numbered page choices before requesting image preparation. It never silently chooses pages.
- FR-AN4: Until turn admission and context handling consume attachment selections, selecting a file visibly prevents sending it as a text-only turn. This slice does not claim parent T050 completion.
