# Image schema rollout and rollback

This slice adds private image job, event and preset tables. Image admission remains disabled.

## Inspect

Check Alembic revision `0023_image_jobs_presets`. Inspect counts in `image_presets`,
`image_preset_revisions`, `image_jobs` and `image_job_events`; new tables should be empty
until image admission is explicitly enabled in a later feature slice. The migration does not
alter text or VLM records.

## Roll back

Keep image admission disabled. Before downgrading, ensure workers are drained, cancel or
reconcile any image jobs, and back up all image tables. The downgrade refuses to drop tables
while any image record exists; it never deletes records on its own. Blob cleanup is separate
and must follow the image storage runbook when that service ships. An empty schema can be
downgraded from revision `0023_image_jobs_presets` to `0022_stopped_usage_outcome`.

Revision `0024_image_assets` adds `image_inputs`, `image_outputs`, `image_transfers` and
`image_download_grants`. Inspect their counts and expiry state before rollback. It refuses
downgrade while any asset record remains. Revoke grants, reconcile transfers, physically
purge private blobs, and back up asset records before removing them from the database.
Only then downgrade to `0023_image_jobs_presets`; it also removes the owner-pair key added
to `image_jobs`. Never use a schema downgrade to delete private blobs.

Revision `0025_image_capacity` adds quota counters, execution leases and coexistence
profiles. Inspect `image_quotas`, `image_execution_leases` and
`image_coexistence_profiles`. Before rollback, stop admission and reconcile every active
lease with node process/PID evidence. A timeout alone is not proof a worker stopped.
Back up the rows; downgrade to `0024_image_assets` only after all three tables are empty.

The migration chain was validated against disposable local PostgreSQL 17 by
`apps/coire-api/tests/unit/test_image_migration_postgres.py`. Set
`COIRE_TEST_POSTGRES_DSN` to a loopback PostgreSQL admin DSN; the test creates and drops
its own random database. It verifies populated downgrade refusal and old text/VLM row
survival. Operator-run cluster migration evidence is still required before rollout.
