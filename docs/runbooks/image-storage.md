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

## Stop and roll back

Keep or restore `COIRE_IMAGE_ENABLED=false`. Drain/cancel image workers and reconcile
pending transfers before changing mounts. Revert the Compose and Nginx change to restore
the previous topology; preserve the volume until owner records, tombstones and physical
purge are reconciled. Never remove the volume as a schema rollback shortcut.
