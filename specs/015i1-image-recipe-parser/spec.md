# Feature Specification: Settings-only PNG recipe parser

**Feature Branch**: `feat/015i1-image-recipe-parser`
**Parent**: `specs/015-image-generation/` (part of T012, T050, T053)
**Dependency**: draft PR #59

## Goal

Extract the canonical `coire.image` recipe from an owned recipe-only PNG up to the 64 MiB output limit. Never decode pixels, load the whole image, or allow this parser to turn the upload into a generation input.

## Acceptance

1. A PNG larger than the 10 MiB generation input limit but within 64 MiB yields its valid recipe. A file larger than 64 MiB is refused before parsing.
2. The parser validates signature, chunk framing/order, CRCs, one uncompressed iTXt `coire.image` entry, and the strict `ImageRecipe` schema.
3. Recipe metadata above 64 KiB, compressed or duplicate recipe entries, malformed chunks, invalid UTF-8/JSON, missing recipe, and links/nonregular files fail with stable content-free error codes.
4. IDAT bytes are streamed in small bounded blocks without Pillow or pixel decompression. Route/upload storage integration remains parent work.
