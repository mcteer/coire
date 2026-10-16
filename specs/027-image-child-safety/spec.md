# Universal image child-safety restriction

User instruction: block sexual content involving minors for every account, including administrators, then continue feature 026.

FR-001: Reject prompts combining a minor/youth signal with sexualization or nudity, in every content mode and for every principal, before queuing, reserving, or invoking inference. Explicit mode itself is a sexual-content signal for this conservative guard. No entitlement, role, preset or API flag bypasses policy.
FR-002: Recheck resolved presets and saved idempotent snapshots; apply the same policy before worker generation and publication. Refuse gallery/download of known violating legacy recipes without deleting user records.
FR-003: Unclassified outputs are unavailable to standard accounts. Existing normal classification must have valid provenance.
FR-004: Refusals use a stable typed error, audit without raw prompt content, bounded telemetry and an alert/dashboard signal. Test policy and all admission routes with no abusive image generation.
FR-005: Clearly document limitations: deterministic text screening is defense in depth, not reliable age inference or comprehensive semantic/image moderation. Do not claim reference-image abuse or implicit/obfuscated prompts are solved. No unreviewed external service or model acquisition.

Acceptance: role/mode matrix, Unicode and age normalization, preset merging, replay, legacy download, harmless control prompts, no generation/capacity on refusal, generated OpenAPI, production refusal check with mocked screening verdict and no inference. Preserve permitted adult and nonsexual requests.
