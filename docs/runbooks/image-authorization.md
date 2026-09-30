# Image authorization preparation

Image admission is disabled. The shared image guard accepts only active user/admin
identities or personal user-bound API keys with the `images` scope. Browser mutations
require the configured exact Origin. Explicit actions additionally need a live `explicit`
entitlement, and personal keys need `images:explicit`. Current user, key and entitlement
rows are refreshed under transaction locks at each action boundary; a cached principal
claim cannot restore a revoked grant.

To inspect a refusal once routes ship, check safe image error codes and the audited
actor/route, without collecting prompts, recipes or credentials. Keep admission disabled
to stop new work; use cancellation and revocation procedures as those services arrive.
This slice does not add routes, grant or revoke authority, owner-row filtering or audit
paths; those remain required before activation.
