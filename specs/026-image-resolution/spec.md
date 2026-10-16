# Feature Specification: Image resolution and aspect ratios

**Feature Branch**: `feat/026-image-resolution`
**Created**: 2026-10-16
**Status**: Implementing
**Input**: User requires image generation up to 1980×1980 and aspect-ratio support; the current 512×512 ceiling was not requested.

## User Scenarios & Testing

### User Story 1 — Larger images (Priority: P1)
An entitled user chooses a useful output resolution and receives an image of those
exact dimensions, up to 1980×1980 on the published image model.
**Independent Test**: Generate 1980×1980; download and inspect its pixel dimensions.
**Acceptance Scenarios**:
1. A published qualified model offers sizes beyond 512 pixels, including 1980 square.
2. The final PNG and saved/reused recipe retain the chosen dimensions exactly.
3. A failed or unmeasured qualification does not advertise larger sizes.

### User Story 2 — Portrait and landscape (Priority: P1)
A user selects square, portrait or landscape proportions without calculating width
and height manually, while retaining a custom-size option.
**Independent Test**: Select 16:9 and 9:16, generate both and verify dimensions/ratios.
**Acceptance Scenarios**:
1. Common 1:1, 4:3, 3:2, 16:9 and reciprocal portrait ratios update the displayed dimensions.
2. Choices respect the model's measured limits and clearly display exact output size.
3. Reuse/import preserves dimensions; changing the ratio deliberately updates both sides.

### User Story 3 — Qualification and operational safety (Priority: P1)
An administrator can qualify expanded sizes on already acquired copies without
redownloading models or bypassing memory reservations and audit.
**Independent Test**: Qualify both copies, verify measured bounds and rollback a failed attempt.
**Acceptance Scenarios**:
1. Only administrators can request qualification; it is audited and observable.
2. Success records both-copy measurements before expanding the published capability.
3. Failure leaves the previous validated capability usable and releases reservations.

### Edge Cases
Exact 1980 is not an engine-aligned dimension; output must remain 1980 rather than
silently rounding down. Extreme aspect ratios, oversized pixel counts, odd sizes,
input images, cancellation, interrupted qualification, retries, output metadata and
existing 512-pixel recipes must remain validated and deterministic.

## Requirements
- **FR-001**: Support exact requested final dimensions through 1980×1980 on the qualified published base model.
- **FR-002**: Offer resolution and common aspect-ratio controls plus custom width/height, with truthful supported limits.
- **FR-003**: Preserve exact output dimensions in downloads, metadata, import/reuse and regeneration.
- **FR-004**: Qualify expanded square, landscape and portrait execution on both existing model copies within reserved memory before advertising support.
- **FR-005**: Qualification must be authenticated, scoped, audited, restart-resumable and reversible without model acquisition or loss of prior valid bounds.
- **FR-006**: Preserve cancellation, entitlement, private output storage, recipe validation and chat/image isolation.
- **FR-007**: Explain any internal alignment handling and never silently change requested final output dimensions.
- **FR-008**: Include contract/unit tests, real Studio acceptance, and headless WebKit inspection of resolution/ratio controls in both appearances and mobile.

### Key Entities
Image dimensions: requested output width/height and derived internal aligned canvas.
Measured capability: validated dimension/pixel limits and resource evidence for both copies.
Qualification: administrator-owned bounded validation request, durable status and audit.

## Success Criteria
- **SC-001**: A 1980×1980 requested output downloads with exactly 1980×1980 pixels.
- **SC-002**: Landscape and portrait ratio outputs match their displayed dimensions and preserve ratios exactly.
- **SC-003**: Every advertised expanded limit has successful both-copy evidence; failed attempts preserve previous bounds.
- **SC-004**: Resolution/ratio controls show no clipping or horizontal overflow at 390px and desktop widths in both themes.

## Assumptions and Scope
1980 is taken literally, not replaced by 1920 or 1984. Common exact ratios may use a
slightly shorter long edge to fit whole pixels (e.g. 1968×1107 for 16:9 at a 1980
limit), and the actual dimensions are displayed before submission. The first rollout
qualifies the currently published Z-Image base; other models retain their own measured
capabilities. No new engine, wrapper, dependency, model acquisition or license choice.

## Expanded image workspace requirements (2026-10-16)
User requests also require replacing the oversized left Image column with a compact,
understandable workbench; guided text, transform, area-edit and reference workflows;
permanent deletion of selected generation history events; large image previews;
small thumbnail overlay actions; and multi-image selection with bulk deletion.
Existing color themes remain applicable. Only supported model workflows are enabled.

- **FR-009**: Compact prompt workbench and readable mode names explain required source,
  mask or reference inputs; advanced controls are disclosed rather than overwhelming.
- **FR-010**: Clicking a library thumbnail opens a large accessible preview with close,
  download and keyboard navigation/focus handling; private assets remain authenticated.
- **FR-011**: Thumbnail actions use small labelled icons visible on hover/focus and
  available on touch. Selection controls support bulk permanent image deletion with
  confirmation and honest reporting of partial failures.
- **FR-012**: Owners can permanently delete selected terminal generation history and
  its event records through a scoped API. Library images remain unless separately
  deleted. Security audit retention and necessary image provenance remain intact.
- **FR-013**: Design decisions are based on inspected established image interfaces;
  implementation is visually inspected in headless WebKit at desktop/mobile sizes,
  including populated galleries, previews, selection, guided modes and job progress.

Acceptance includes keyboard/touch access, owner isolation, real deletion contracts,
partial failure handling, no overflow, and cleanup of agent-created test data only.

- **FR-014**: Expanded base-model qualification must not implicitly expand an auxiliary
  workflow. Advertise and enforce mode-specific bounds: text/transform use measured 1980px
  capability; currently qualified Canny control retains its 512px validation bounds.

- **FR-015**: Library cards contain only the thumbnail and overlay controls, without
  generation identifiers or timestamps beneath them. The preview must contain the
  entire image at any supported aspect ratio, including 1980px originals, within the
  viewport while keeping close and download controls visible.
