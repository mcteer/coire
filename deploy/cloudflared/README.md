# Failover tunnel templates

These files are declarative templates, not credentials. Provision one Cloudflare Tunnel per host
and one pool per tunnel in the Access-protected public Load Balancer. The LB monitor must probe election readiness without caching and regard only HTTP 200 as healthy.
Core's probe is `/failover/ready`. Each Studio probe is `/ready` on `coire-failover`. The fallback
pool is intentionally empty: a stale, draining, or unelected member must never receive ingress.
The Studio tunnel runs on its host and reaches the failover container through the loopback-only
`127.0.0.1:8004` compose publication. No Studio API port is published on the VLAN.

Render the tunnel UUID and credentials from Keychain-backed deployment secrets. Do not commit the
rendered `config.yml`, tunnel JSON credential, Cloudflare API token, or load-balancer identifiers.
