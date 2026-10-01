# Image preset templates

These examples are inert until a human administrator binds `__MODEL_ID__` to the UUID
of a ready, measured image base in Coire's registry. Work on a local copy of a JSON
file and replace the slot with that UUID. Submit the resulting `ImagePresetCreate`
body to `POST /api/v1/admin/image-presets` using an authenticated human-admin session
and the configured browser Origin. The API checks the current base and hidden
auxiliary dependencies, stores revision 1 and writes a content-free audit row in the
same transaction. Inspect the returned preset ID/revision and `image.preset.create`
audit event before using it.

These files contain no entitlement, model path, repository URL or acquisition
instruction. Loading or posting a template never downloads weights. Use the existing
admin acquisition pipeline to obtain and verify an image model first. Preset defaults
are deliberately partial; the eventual submit form must supply a prompt and values
within that model's measured capability profile. Retire a preset through the audited
admin DELETE route to stop new use while preserving revision history.
