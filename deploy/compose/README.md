# Core compose deployment

`coire-up` resolves `coire-core` through UniFi DNS and records the observed bind address in the
gitignored `.env`. Port 8080 and OTLP 4317 bind only to that control address. Override with
`COIRE_CONTROL_BIND_ADDRESS` only during a reviewed recovery; never use `0.0.0.0`.

Secrets are materialised from Keychain under `~/.coire/secrets` and mounted as files. Use
`coire-down` to remove them. The integration override creates a shared control network and an
internal Studio-only data network; core is deliberately absent from the latter.
