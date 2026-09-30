# Image registry isolation

The existing `/api/v1/models`, `/v1/models`, chat, vision and MCP model selectors accept
only `mlx_lm` or `mlx_vlm` registry backends. The direct gateway resolver also checks
this before looking for an engine. Failover snapshots already select only `mlx_lm`.
Image generation (`mflux`) and auxiliary image assets (`auxiliary`) must be offered
through the future image capability and admin acquisition routes, never these selectors.

The legacy admin `/api/v1/admin/models` add route rejects non-language `kind` values
with an audited `unsupported_kind_in_language_acquisition` reason before inspection.
This prevents a requested image kind being silently stored as a language model. If a
model is absent from Chat, inspect its registry backend and the add-refusal audit.
Do not relabel an image asset as `mlx_lm` to make it appear; use the image-specific
acquisition and validation path when that service is implemented.
