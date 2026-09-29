# Frontier provider Chat routing

Core proxies selected text turns to the official OpenAI or Anthropic API over HTTPS. The selected registry UUID determines the provider and model ID. No weights or user harness run on core. The provider receives the selected turn's plain-text history, including system text; the picker labels the provider before selection. Inline images and tool requests are refused before provider I/O.

## Provision and enable

1. Store the provider credential in the login Keychain as service `coire-openai-api-key` or `coire-anthropic-api-key`, account `coire`. Do not put it in `.env`, the request body or a shell command line. `coire-up` copies the item into an API-only Compose secret. An absent item stages an empty file.
2. Set `COIRE_PROVIDER_CHAT_ENABLED=true` for the core Compose release and run `deploy/compose/coire-up --build`. The flag defaults false. Check the API health endpoint and the mounted credential's availability through a bounded admin acceptance request; never display the credential.
3. POST `/api/v1/admin/provider-models` with `source` (`openai` or `anthropic`), `provider_model_id`, `display_name`, `context_window`, `max_output_tokens` and `daily_token_budget`. Registration is audited and starts `admin_only`. Use the returned registry UUID for all subsequent calls.
4. PATCH `/api/v1/admin/models/{id}` with `visibility: published` after testing, and set the desired entitlement list. Publication requires the provider flag and mounted credential. Keep Code mode on Studio models.

The daily token allowance is global per registered model, measured in UTC days. Each request holds an upper bound on prompt tokens plus maximum output before HTTPS I/O; concurrent holds serialize on the model row. A successful provider usage receipt settles the hold. Failed, cancelled or unreported requests retain the full hold, so the cap fails closed. The API key's monthly token budget also applies. Configure provider-side spend limits separately because provider billing and tokenization are external systems.

## See and stop it

- The Chat picker and `/v1/models` show source. The gateway dashboard has provider request and token-rate panels; `CoireProviderFailures` alerts on failed provider requests. Filter traces for `coire.api.provider.stream` and structured gateway logs by model UUID. Credentials and prompt text are not logged.
- A Chat **Stop** cancels the HTTP stream and records a stopped turn. Closing a compatible SSE response closes the upstream stream. A request with unknown final usage retains its full daily hold.
- To stop future paid calls, unpublish the model with the admin PATCH, or set `COIRE_PROVIDER_CHAT_ENABLED=false` and release the core stack. Remove the Keychain item and restage secrets to revoke the local credential. Revoke the key at the provider too. Existing Studio serving is independent of this flag.

## Acceptance

Use a temporary, low-limit provider key. Register one small text target, make one short `/v1/chat/completions` request and one native Chat turn, verify the returned registry UUID, source label, reported tokens, daily reservation and Stop cancellation, then unpublish it. Record provider-reported cost from the provider console without copying the credential into the repository.
