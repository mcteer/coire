# Image storage preparation

Image admission stays disabled by default. The API is the sole owner of the
`coire-blobs` volume at `/opt/coire/blobs`. The file worker processes image inputs in
dedicated `images` subpaths under the existing chat original/derived volumes; it does not
mount final blobs. The scheduler has no blob mount.

## Inspect

Render production and integration Compose configurations and verify `IMAGE_ENABLED=false`.
Check the API-only blob mount, existing file-worker original read-only mount, and the
route-specific Nginx body limits. Once services exist, inspect quota rows, transfer receipts
and disk safety floor before any admission change. Do not log or expose blob paths.

The isolated file worker now has a settings-only `parse_recipe_png` helper for future
owner-scoped recipe uploads. It accepts regular PNG files up to 64 MiB, validates PNG chunk
framing and CRCs, and extracts at most 64 KiB of uncompressed `coire.image` iTXt JSON into
the strict `ImageRecipe` model. It never decodes IDAT or promotes the upload to a generation
source. Inspect only its stable error codes (`invalid_png`, `invalid_recipe`,
`recipe_too_large`, `unsupported_recipe_encoding`, `duplicate_recipe`,
`missing_recipe`, `recipe_input_too_large`, `recipe_input_unavailable`). The API upload path
must stage an immutable owner-scoped file and enforce the purpose-specific limits before
calling it; that route is not yet enabled.

The private output gallery metadata routes are now available at
`GET /api/v1/image-outputs` and `GET /api/v1/image-outputs/{id}`. They require a live
human or personal image key, return only the caller's published, nondeleted records,
and include no blob path or content URL. The opaque cursor carries owner and position;
it remains valid if the boundary record is deleted. The gallery remains readable when
new image admission is disabled. Inspect the `gallery` operation in
`coire_image_requests_total` for success/refusal rates and the `coire.api.image.gallery`
span for request timing. Output deletion is still pending.

Owners can now issue five-minute download grants via
`POST /api/v1/image-outputs/{id}/download-grants`. The response URL has a `#grant=`
fragment. Clients must parse that fragment locally and send the token as
`X-Coire-Image-Grant` with their normal credentials to
`GET /api/v1/image-outputs/{id}/content`; navigating to the URL alone cannot fetch
bytes. Do not put the token in a query string or log/capture this header. The API
rechecks owner, current key and explicit entitlement at issue and redemption, then
opens and hashes the blob under the configured root without following symlinks.
Content and grant responses are `private, no-store`. Investigate failed downloads via
the fixed-label `download` operation metric and `coire.api.image.download` span; the
refusal details and blob key are never exposed. Keep image admission disabled to stop
new output; existing owner downloads remain available. A rollback removes these
routes but preserves existing grant rows until the maintenance sweep is implemented.

## Stop and roll back

Keep or restore `COIRE_IMAGE_ENABLED=false`. Drain/cancel image workers and reconcile
pending transfers before changing mounts. Revert the Compose and Nginx change to restore
the previous topology; preserve the volume until owner records, tombstones and physical
purge are reconciled. Never remove the volume as a schema rollback shortcut.
