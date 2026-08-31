# Coire control-plane compose

Runtime configuration is supplied through `COIRE_` environment variables and Keychain-sourced
compose secrets. Gateway tuning variables and operational procedures are documented in
[`docs/runbooks/gateway.md`](../../docs/runbooks/gateway.md). Do not put credentials in this file,
`.env`, an image, or a compose environment block.
