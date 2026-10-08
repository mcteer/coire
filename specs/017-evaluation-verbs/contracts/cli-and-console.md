# CLI and Console Contract

## CLI

Keep global authentication/API URL handling and existing `coire eval harness VARIANT_UUID [--engine-version VERSION] [--adapter ADAPTER_UUID]` syntax. Its implementation now submits a durable Studio execution and waits by default; it never sends prompts from the local CLI. The legacy `--engine-version` becomes an optional expected-version assertion (the old `unknown` default makes no assertion); measured runtime provenance is always authoritative. The standard `harness-capability` v1 suite is installed by the migration/catalog bootstrap with system audit attribution. Admins register task/judge suite versions explicitly from supported templates; acceptance registers the named v1 suites.

Add the following bounded verbs, with JSON output available and secrets supplied through the established credential path:

```text
coire eval suites templates
coire eval suites list
coire eval suites register --file SUITE_JSON --idempotency-key KEY
coire eval task --suite ID --suite-version N --model UUID --variant UUID [--adapter UUID] [--against-base] [--training-job ULID] [--no-wait]
coire eval judge --suite ID --suite-version N --model UUID --variant UUID [--adapter UUID] [--against-base | --against-model UUID --against-variant UUID --against-adapter UUID] [--no-wait]
coire eval list [--job ULID | --adapter UUID]
coire eval show ULID
coire eval wait ULID
coire eval cancel ULID --expected-version N --idempotency-key KEY
coire eval rerun ULID --expected-version N --idempotency-key KEY
coire eval compare --left RESULT_ID --left-subject N --right RESULT_ID --right-subject N
coire eval measure --file MEASUREMENT_JSON --idempotency-key KEY
coire eval measurement UUID
coire eval measurement-cancel UUID --expected-version N --idempotency-key KEY
```

Submit commands accept `--idempotency-key`; generate a fresh UUID key when omitted and print the accepted ID/key so a transport retry can reuse it. A rerun never silently reuses the old key. `--against-base` requires an adapter and resolves its exact base variant; external comparison target flags require model and variant together, with adapter optional. Rubric accepts one/two subjects; pairwise requires two. Fixed judge comes from suite registration and cannot be overridden by a CLI flag.

Exit 0: a terminal measured execution or successful query, including a valid low task/judge score; harness measured failure retains nonzero status. Exit 1: measured harness failure or failed/timed-out/cancelled evaluation; exit 2: invalid invocation/auth/transport problem. `--no-wait` exits 0 on accepted submission and labels it pending. Waiting displays persisted phase/cleanup status with bounded polling/SSE; interruption stops waiting without silently cancelling server work. `coire data analyze` remains unchanged.

## Console

Extend the existing admin training and model detail surfaces; keep the shipped design's run list, center detail and right dataset/adapter panel. New components in `src/components/evaluations/` provide the following:

- Suite selector and submit form using registry model/variant/adapter IDs; judge identity is read-only from its suite version. Reject self-judge before submit when known and show authoritative API refusal afterward.
- Training form opt-in suite picker and explicit checkpoint updates; selecting suites creates schema v2. Existing form/recipe loading preserves v1 when undeclared and never changes held-out-loss control meaning.
- Adapter comparison with base/candidate score columns, task/rubric delta, per-case drilldown and provenance; pairwise shows wins/ties/losses. Failed/pending/non-comparable/contamination/evidence-expired states have distinct text, never a zero placeholder. Task/judge panels do not show a verification/publish switch.
- Run history and detail with suite version, exact judge, timestamps, outcome, bounded private evidence access, rerun and versioned cancel controls. Retired targets remain inspectable and disabled for new runs with a reason.
- Checkpoint scores plotted at completed update and attempt alongside existing loss. Superseded/recovered attempts are labeled; no fabricated uninterrupted curve. An evaluation-owned training pause is visibly different from an admin/protective pause.
- Separate evaluation/group subscriptions continue after training reaches terminal state. Use existing `useEventStream` and replay/reset snapshots; do not fetch directly from components. Only generated API response types in `src/api/evaluations.ts`.

Accessibility: keyboard-operable selection/actions, labeled score tables, text alternatives for curves, focus/error feedback, no state conveyed only by color. Component tests cover screen-reader labels, reconnect, partial outcomes and unauthorized access. Update `docs/design/DESIGN.md`'s 017 placeholder only when implementation is delivered; preference optimization stays outside scope.
