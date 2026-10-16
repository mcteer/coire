# Data model

`local_credentials`: user_id FK/PK, password_hash, generation, updated_at.
`browser_sessions`: digest PK, user_id FK/index, generation, created_at, expires_at/index, revoked_at.
`local_login_windows`: account digest PK, window_start, attempts. One fixed window row per known credential; unknown accounts share a sentinel bucket, avoiding unbounded attacker-controlled row growth. Lock before increment; commit refused attempts independently. Reset locks credential and increments generation, revoking all old sessions atomically.
