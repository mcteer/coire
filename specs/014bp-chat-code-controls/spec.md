# Feature Specification: Chat Code Controls

**Feature Branch**: `feat/014bp-code-web-controls`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The browser offers a separate Code conversation mode with Research, Plan and Apply actions. The user chooses an owner registered repository, source revision and, where relevant, a prior Research or Plan result. Apply identifies its plan explicitly and explains that it starts with a fresh workspace at that plan's revision. The model picker includes only eligible published coding models and requires harness verification for Apply. A running code turn shows bounded live tool activity, result, test status, diff excerpt and a time-limited owner bundle download. Stop uses the existing owner-scoped run cancellation. Browser workspace writes require the configured exact origin; API-key access remains available.

This slice covers parent T043. Full coding integration on a Studio remains parent T044.
