# Research

Existing identity keys already use argon2-cffi with Argon2id at 19MiB/two iterations/one lane; reuse it with bounded off-thread work. Database authoritative session lookup ensures role/entitlement/deactivation changes take effect immediately. SHA256 is appropriate for high-entropy opaque session tokens, not passwords. Exact configured origin plus Host verification protects login and all cookie writes. Existing Chat origin validator already permits HTTPS and loopback HTTP only; reuse it.

Sources: [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) and [OWASP CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html). Secure HttpOnly SameSite=Strict cookie, path=/, no Domain; loopback HTTP uses a distinct development cookie name.

No Cloudflare account work is in scope. No new ports/network permissions. Private operator reset requires host access and audits each change. No unauthenticated bootstrap route.
