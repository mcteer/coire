# Image registry isolation

The existing `/api/v1/models`, `/v1/models`, chat, vision and MCP model selectors accept
only `language_model` registry rows with `mlx_lm` or `mlx_vlm` backends. The direct
gateway resolver also checks this before looking for an engine. Failover snapshots
select only `language_model` rows on `mlx_lm`.
MCP's coding selector now applies the same kind/backend check before consulting
`coding` tags or validated variants; a tag cannot promote an image asset into a run.
The signed failover publisher filters in both its SQL query and final projection.
Image generation (`mflux`) and auxiliary image assets (`auxiliary`) must be offered
through the future image capability and admin acquisition routes, never these selectors.

The legacy admin `/api/v1/admin/models` add route rejects non-language `kind` values
with an audited `unsupported_kind_in_language_acquisition` reason before inspection.
This prevents a requested image kind being silently stored as a language model. If a
model is absent from Chat, inspect its registry backend and the add-refusal audit.
Do not relabel an image asset as `mlx_lm` to make it appear; use the image-specific
acquisition and validation path when that service is implemented.

Migration `0026_image_registry_kind` fills existing rows as `language_model` and
constrains every kind to its permitted backend and Studio source. Before rolling back
to `0025_image_capacity`, retire and remove all non-language registry rows using the
future image-asset drain procedure; the migration refuses a downgrade while any remain.
Check `SELECT kind, backend, source FROM models` during a staging rollout. The local
disposable PostgreSQL migration test covers old text/VLM rows, the kind constraint and
guarded rollback.

Migration `0027_image_capability_profile` adds a separate nullable JSONB profile for
measured image base limits. A ready `image_model` row must have one; all other kinds
must leave it null. The admin image validation path must parse the stored value through
`ImageCapabilityProfile` before moving an image base to ready. A downgrade to 0026
refuses while any profile is stored. Drain or remove those assets through the future
admin image path before rollback; never clear a live profile merely to force migration.
