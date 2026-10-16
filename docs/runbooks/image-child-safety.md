# Image child-safety enforcement

Sexual content involving minors is prohibited for all identities. Administrator roles,
explicit entitlements and API scopes never exempt a request from the content policy.
The shared `child-safety-text-v1` check runs before admission work, after preset resolution,
on idempotent replay, at native prompt/generation entry, before publication and on legacy
recipe reads. Refusals return `image_content_blocked` (403). API refusal audit contains no
raw prompt; watch `coire_image_content_policy_refusals_total` and the image dashboard/alert.

Standard users cannot read unclassified outputs without explicit entitlement. A tag or
explicit entitlement is never a child-safety verdict. Known violating legacy recipes are
hidden/refused, not automatically deleted; security audit records remain append-only.

## Limits
This change implements conservative text screening, not comprehensive moderation. The
pinned NSFW classifier does not identify age. Implicit intent, unsupported languages,
unrecognized obfuscation and image-only/reference-input abuse are not reliably detected.
Do not advertise the platform as child-safe on the basis of these checks. A reviewed
semantic input and output moderation stage with a fail-closed unavailable state is still
required for that assurance. There is no admin bypass or test-generation exception.

## Verification and response
Run shared policy and API authorization/admission tests. Use synthetic text, mocked
verdicts and harmless controls; never generate abusive test images. Inspect refusal audit,
ensure no job/reservation/engine work was created, and verify admin and standard principals.
For unsafe behavior outside these checks, stop image admission through deployment config
`IMAGE_ENABLED=false` and replace only API; follow the image runbook for draining/cancelling
already admitted work. Do not delete user data or weaken audit as part of rollback.
