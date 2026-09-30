# Image preset resolution

Image admission remains disabled. The preset resolver accepts only a published current
revision. It rejects retired presets, stale revision requests and attempts to rebind
the preset to another base model. It overlays explicitly supplied request fields,
applies the stored prompt prefix once and retains explicit mode when the preset or
one of its dependencies requires it. Its result includes the frozen dependency IDs
and the union of preset and current registry entitlement names.

When admission routes are added, load the preset and registry dependencies in the
same authorization transaction, call the resolver, then check its returned
requirements against the user's live entitlements. A missing dependency requirement
entry is an invalid request; do not use request or imported recipe fields to grant
entitlements. Retiring a preset blocks new admissions while existing job revisions
remain in the database. If a preset appears stale or unsafe, retire it through the
future audited admin route; the mutation service is not exposed by a route yet.

`load_resolved_image_preset` now performs that database lookup and policy recheck in
the caller's transaction. It locks the published preset pointer, immutable revision,
and every current registry dependency. It requires a ready Studio image base with a
valid measured capability profile; auxiliary kinds must match their requested roles.
It then checks the union of frozen preset and current registry entitlements against
the live user/key. A missing or malformed row fails before worker dispatch. The
future admission route must keep this transaction open through job/quota/audit commit
and must audit service-level refusals; no route calls the loader yet.

The base model's measured `required_dependency_ids` are also loaded under the same
locks, even when they are absent from the visible preset fields. Their current
registry entitlement requirements join the admission union; an override cannot
remove them. A missing required asset blocks the request. Admin acquisition must
populate this list from validated local component manifests, using registry UUIDs.

`admin_presets` now has transaction-scoped create, update and retire operations for
human-admin routes to call. Create publishes revision 1 only after checking a ready
measured base and every visible/hidden auxiliary. Update locks the pointer, requires
the expected revision and inserts a new revision; old revisions are never edited.
Retire changes only the pointer state, preserving job history. Each successful
mutation writes a content-free `image.preset.*` audit row in the same transaction.
`POST /api/v1/admin/image-presets`, `PATCH /api/v1/admin/image-presets/{preset_id}`
and `DELETE /api/v1/admin/image-presets/{preset_id}` now require a live human admin
and exact configured Origin on writes. They commit the pointer, immutable revision
and success audit together. A refusal writes `image.preset.admin_refused` separately;
stale revisions and duplicate names return a safe conflict. Inspect those audit
actions and the `coire_image_requests_total{operation="preset_mutation"}` counter
without recording prompt content. To stop new use, retire the pointer; do not delete
revision rows while jobs or outputs reference them. The feature flag still prevents
image admission, and ordinary eligible preset listings remain to be wired.
