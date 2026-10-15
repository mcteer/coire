# Deferred Cloudflare deployment design

The owner has deferred Cloudflare installation and public-edge qualification until last.
These files describe intended host routing; `load-balancer.yaml.tmpl` is a Coire design
manifest, **not a supported Cloudflare API payload or a deployable configuration**. In particular,
`steering: priority` and `fallback_pool: null` are unresolved design placeholders. Do not apply
or claim provider support from these fields. T056/T057 and public acceptance remain open.

The later deployment must verify the actual account entitlement and supported pool ordering,
configure a non-serving all-unhealthy fallback, and measure routing/propagation with authenticated
monitors. Core readiness is `/failover/ready`, Studio readiness `/ready`; require HTTP 200,
no caching/redirects and the scoped `X-Coire-Failover-Monitor` secret. Monitor credentials cannot
access inference. Browser routes/assets retain Access JWT validation even at a direct origin.
No provider failure may make a stale application or node relay admit a request.

Intended topology remains one outbound tunnel per host. A Studio tunnel reaches its frontend at
loopback-only `127.0.0.1:8004`; no Studio API port is published on the VLAN. Render approved tunnel
credentials and monitor secrets from Keychain-backed deployment secrets outside Git. Do not commit
rendered configurations, tunnel JSON, API tokens or account identifiers. Any network/Access
expansion still requires separate owner approval.
