# Local authentication API

GET /api/v1/auth/local: non-secret configuration (enabled, external sign-in availability); no identity data.
POST /api/v1/auth/local/login: email/password JSON; exact Origin/Host required; 200 session expiry and User, HttpOnly cookie; uniform 401, bounded 429 with Retry-After, 403 origin refusal, 404 disabled.
POST /api/v1/auth/local/logout: exact origin and valid session required; revoke then 204 and delete cookie.
GET /api/v1/me: unchanged User contract; now also accepts valid local session.
Operator-only provisioning/reset: hidden prompt, existing enabled user, no public registration or role changes.
