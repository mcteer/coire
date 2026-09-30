# Image authorization preparation

Image admission is disabled. The shared image guard accepts only active user/admin
identities or personal user-bound API keys with the `images` scope. Browser mutations
require the configured exact Origin. Explicit actions additionally need a live `explicit`
entitlement, and personal keys need `images:explicit`. Current user, key and entitlement
rows are refreshed under transaction locks at each action boundary; a cached principal
claim cannot restore a revoked grant.

The reusable `CurrentImageUser` FastAPI dependency applies that preflight and live
recheck at the route boundary. A refusal writes `image.refused` in a separate audit
transaction with the actor, method/path and fixed reason `authorization`; it never
records prompts, recipes or credentials. Ordinary job, input and published output
lookups return `image_not_found` for other owners, including when the caller is an
administrator. Deleted inputs and unpublished or deleted outputs are also hidden.

To inspect a refusal once routes ship, check the safe image error code and
`image.refused` audit actor and route. Keep admission disabled to stop new work; use
cancellation and revocation procedures as those services arrive. Production image
routes, admission/completion audits and grant/revoke authority remain required before
activation.
