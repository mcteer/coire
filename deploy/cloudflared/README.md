# Cloudflare ingress deployment

`load-balancer.yaml.tmpl` contains separate resource request bodies, grouped by local
labels. It is not one API request. Render outside Git with Keychain-backed secrets:
create each `monitors` value at the account monitor endpoint, substitute returned monitor
IDs into each `pools` value, create the pools at the account pool endpoint, then substitute
pool IDs into `load_balancer` and create it at the zone load-balancer endpoint. Never send
the grouping keys to Cloudflare. Account IDs, tunnel credentials and rendered secrets stay
outside Git. No provisioning command runs automatically from these templates.

The proxied load balancer uses `steering_policy: off` with default pools ordered Core,
Studio A, Studio B. This is Cloudflare's documented ordered failover policy. The separate
fallback pool AND its origin are disabled; it is excluded from the
normal pool order. Cloudflare ignores fallback health, so an enabled unhealthy application
pool is unsafe as the fallback. With all normal pools unhealthy, a disabled fallback and
proxied hostname produce HTTP 530 / origin DNS error 1016. Neither an origin with stale
proof nor a node relay may admit traffic even while provider health has not converged.

Monitors use HTTPS, verified TLS, exact status `200`, no redirects, a five-second timeout
and no retry. Core readiness is `/failover/ready`; Studio readiness is `/ready`. Each host
receives its own scoped `X-Coire-Failover-Monitor` secret. The monitor request disables
caching and the application returns `Cache-Control: no-store`. Inspect account cache rules
and ensure these readiness paths are never cached before enabling public routing. Do not
replace readiness with nginx liveness. Monitor credentials cannot access inference.
The 15-second interval requires Business or Enterprise entitlement; verify it before
creation. A Pro account's 60-second minimum does not establish the feature's failover bound.
No account upgrade or purchase is authorized by this template.

Preserve the existing Access application for the public hostname. Access is configured
separately from the load-balancer API; `access_application` is not a load-balancer field.
If the existing policy blocks probes, prepare a narrowly scoped monitor service identity
and its private headers for review. Do not bypass Access or broaden a policy automatically.
Browser routes/assets also validate Access JWTs at the direct origin. Measure public
propagation, authenticated readiness, all-unhealthy refusal, stale-origin refusal, and
recovery with the actual account before claiming provider acceptance.

Intended topology is one outbound tunnel per host. The Core template targets the web
container on its existing network; run its connector on that same concern network. Studio
templates target the host's loopback-only frontend at `127.0.0.1:8004`, so their connector
must run in the native host namespace. A container's loopback does not reach that frontend.
No Studio API port is published on the VLAN. The credential-file placeholders must be privately rendered to the actual connector's
readable Keychain-sourced credential path;
do not create world-readable directories on macOS or introduce host networking to make
an unreviewed container deployment work. Compose intentionally installs no connector yet.
Any network or Access expansion requires separate owner approval after a concrete review.

The owner reaffirmed on 2026-10-15 that installation and public acceptance stay deferred
while local implementation and first local platform use take priority. Do not request
account setup or resume rollout without a later owner instruction. The resource
bodies and local contract tests are prepared; account entitlement, tunnel installation,
Access-policy inspection and measured public acceptance remain rollout prerequisites.
No provider acceptance is claimed from template tests.

Provider references:

- [Monitor API](https://developers.cloudflare.com/api/resources/load_balancers/subresources/monitors/methods/create/)
- [Pool API](https://developers.cloudflare.com/api/resources/load_balancers/subresources/pools/methods/create/)
- [Load-balancer API](https://developers.cloudflare.com/api/resources/load_balancers/)
- [Ordered steering](https://developers.cloudflare.com/load-balancing/understand-basics/traffic-steering/steering-policies/standard-options/)
- [Disabled fallback behavior](https://developers.cloudflare.com/load-balancing/understand-basics/health-details/)
- [Monitor interval entitlements](https://developers.cloudflare.com/load-balancing/monitors/create-monitor/)
- [Tunnel origins](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/public-load-balancers/)
