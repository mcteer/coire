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
future audited admin route; this slice does not expose a mutation endpoint.
