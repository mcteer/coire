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
