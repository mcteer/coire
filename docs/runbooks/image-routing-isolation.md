# Image registry isolation

The existing `/api/v1/models`, `/v1/models`, chat, vision and MCP model selectors accept
only `language_model` registry rows with `mlx_lm` or `mlx_vlm` backends. The direct
gateway resolver also checks this before looking for an engine. Failover snapshots
select only `language_model` rows on `mlx_lm`.
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
