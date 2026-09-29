# Feature Specification: Provider-Agnostic Chat Routing

**Feature Branch**: `feat/014bw-frontier-chat-routing`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The core-hosted Chat UI lists all entitled, published, ready models from the registry, whether their execution target is a Studio or an administrator-configured external provider. Initially support official OpenAI and Anthropic frontier APIs through explicit provider adapters, with room for additional allowlisted provider adapters. The selected registry UUID determines the remote model name and provider; callers cannot supply endpoint, credential or provider model name. Credentials come only from Keychain-sourced API secrets. Plain text streaming, Stop, usage, history, attribution, entitlements and audit work consistently. Code-mode agent execution remains on the Studios. External model registration and publication are admin-only, audited and cost-bounded. Unsupported visual or tool features are refused before a billable call.

Native Chat remains default-off until local and external routes pass acceptance. No remote model weights or user harness run on core.
