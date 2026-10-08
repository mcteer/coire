# Durable evaluations

Feature 017 implements fixed harness, task and judge suites. Release acceptance
remains tracked in `specs/017-evaluation-verbs/tasks.md` and
`specs/017-evaluation-verbs/execution-record.md`. Keep new admission disabled until
all required gates pass.

## Observe

Use the admin Training → Evaluation runs view, or `coire eval list`,
`coire eval show RUN_ID` and `coire eval group GROUP_ID`. Job and adapter rows link
declared obligations, including checkpoint update, producing attempt/fence,
current pause owner and resume disposition. Training success, harness verification
and informational task/judge scores have independent states.

The Jobs dashboard publishes outcome counts, pending age, cleanup past deadline
and baseline freshness. The scheduler polls durable state every five seconds even
when evaluation admission and diagnostics are disabled. Idle values are explicit
zeroes; failed polls retain their previous timestamp. Missing/stale baseline is an
alert, not evidence of healthy idle operation. Alerts are
`CoireEvaluationBaselineUnavailable`, `CoireEvaluationCleanupUnresolved` and
`CoireEvaluationAdmissionStalled`.

Tokenizer identity and measurement prompt preparation run in a serial native
subprocess on the Studio. They do not import tokenizers into the node control
process or load model weights. Inspection is bounded by 45 CPU seconds, an
85-second native watchdog, a 90-second parent deadline and 1 GiB RSS; cancellation
kills and reaps the owned child. An inspection failure refuses the evaluation
identity rather than substituting a tokenizer or increasing the node budget.

## Submit and compare

Authenticate with an active human administrator or an admin-scoped API key with
an active human owner. Browser mutations require the configured Origin. Use the
existing Keychain-backed credential sourcing; never put bearer values in argv or
repository files.

List available definitions with `coire eval suites list` and inspect fixed
templates with `coire eval suites templates`. Register a strict definition using
`coire eval suites register --file DEFINITION.json --idempotency-key KEY`.
Definitions are immutable per suite ID/version. Retired suites retain history
and reject new runs.

`coire eval harness VARIANT_UUID [--adapter ADAPTER_UUID]` submits platform
execution; the CLI does not execute a local harness or submit caller-authored
scores. Historical scorecards remain readable. Legacy score POST is refused.
Harness pass requires every capability category to score at least 0.8. A measured
failure revokes the exact target's verification; infrastructure failure preserves
an existing pass. Task/judge scores never grant or revoke verification.

Task and judge submission requires `--suite`, `--suite-version`, `--model` and
`--variant`, with optional `--adapter`, `--against-base` or an exact external
comparison target. A judge is fixed by the registered definition. Same model,
variant family, base-artifact aliases and adapters derived from that base cannot
judge the candidate. Unavailable judges fail explicitly; there is no fallback.
Use `--no-wait` to return the durable receipt immediately. Exit status 0 means a
complete successful execution (low informational scores remain success); 1 means
a failed/cancelled/timed-out execution or measured harness failure; 2 means an
API/auth/transport or wait timeout error.

Rerun explicitly with `coire eval rerun RUN_ID --idempotency-key KEY`; it gets a
fresh ID and leaves previous result digests unchanged. Retry an uncertain mutation
with its original key and expected version. Compare complete results through
`coire eval compare --left RESULT_ID --right RESULT_ID`, selecting subject indexes
when necessary. Incompatible/legacy/failed results have reasons and no delta.
Pairwise preferences retain both presentation orders and disagreements count as
ties; they are not independent quality scores.

## Kill and recover

Cancel with `coire eval cancel RUN_ID --idempotency-key KEY` or the evaluation
detail control. Receipt acceptance is not stop proof. Reachable sandbox stop must
complete within five seconds. Unreachable or uncertain workers retain memory,
private evidence quota and checkpoint pins until authenticated stop and owned
cleanup are proved. Use persisted group/run history to identify the exact owner;
do not delete reservations, kill unrelated engines or remove shared artifacts to
clear an alert.

Only recipe-declared suites run automatically. Every declaration includes final
evaluation; optional pre-final updates must align with checkpoint cadence. A
checkpoint pause is carried by the fenced v2 commit acknowledgement after both
full copies are verified. Every producing rank must stop before training memory
is released and evaluation starts. Extraction, mirroring and serving smoke reuse
the normal native training path. Internal adapters are private, hidden from user
selection/publishing and retired only after all owned workers/engines stop and
both Studios prove file deletion.

Training resumes the exact checkpoint only after cleanup, with live owner
authority and fresh ordinary training admission/profile/resource checks. A newer
administrator or protective action wins. Choose “Keep paused after evaluation”
to retain an explicit admin pause. Resume stays unavailable while cleanup is
unresolved. Exhausted execution or resume deadlines leave a manual pause with a
recorded disposition. No task/judge failure rewrites successful training.

## Evidence and retention

Raw evidence is private under `evaluation-evidence/` in the existing training-data
volume, mode 0700/0600, at most 8 MiB per run and 1 GiB globally by default. Read
it only through the admin evidence route/detail control. Evidence expires after
seven days; immutable score/provenance metadata remains. Symlinks, hard links,
foreign evidence and digest changes are rejected. Do not copy raw candidate/judge
outputs, prompts, datasets or credentials into logs, Git or execution records.

Overlap is computed on actual consumed training inputs through the bound update,
using frozen split/seed/sampler state. Completion changes cannot conceal reused
inputs. Missing or purged input proof is explicitly unavailable.

Ordinary mixed-run guards require at least 30 successful exact-resident latency
samples in the rolling five-minute window, with the newest no older than 60
seconds. A qualified profile does not replace those fresh observations. Keep
authenticated ordinary chat traffic running through phase load/cleanup gaps
when qualifying normal admission. A telemetry-stale refusal invalidates its
profile; repeat explicit measurement before retrying, using a fresh idempotency
key. Prove an authenticated RUNNING child before a live residency perturbation.

## Drain and rollback

Set `COIRE_EVALUATIONS_ENABLED=false` on API and scheduler to close new admission.
Keep the new scheduler/node binaries running until pending obligations become
explicit terminal outcomes and cancellation, private adapter erasure and pin
release finish. Convert remaining evaluation pauses to administrator-owned pauses
through the audited controls; never infer absent processes are stopped. Old v1
training documents and checkpoint bytes retain their original shape.

Binary rollback requires drained workers, engines, imports and pauses. Keep the
additive evaluation history tables when rolling back binaries. Test migration
downgrade only on an isolated metadata copy: the reversible migration refuses
active ownership and retained result/history loss. Restore compatible binaries
before restarting declared-evaluation recipes. Confirm history remains readable
and expired evidence is labeled accurately.

## Isolate container qualification

The disposable container integration overlay uses `192.168.100.0/24` to exercise
the fixed fabric-address contracts. Do not start that overlay in a Studio's host
Docker daemon: its simulated fabric can shadow the real Thunderbolt subnet.
Use CI's isolated runner or a separate Linux guest with its own Docker daemon and
network namespace. A development qualification guest should have no access to
the real Studio control or fabric addresses. Real Studio acceptance continues
through the authenticated deployed gateway and node paths separately.

For a visual judge, runtime attestation binds the registry-selected mlx_vlm backend and its installed package version. Register a new immutable suite version after correcting an old text-only runtime binding; never rewrite a historical suite to match the current process. Full visual-copy checksum verification runs outside the node request loop and engine lifecycle lock; admission and immutable manifest identity are rechecked before spawning.

Visual engine checksum preflight can exceed the registry's short missing-process grace.
Reconciliation defers a missing STARTING engine only while its exact node/engine load command is
running within the existing gateway operation timeout. Completed, failed, absent, or expired
commands retain ordinary missing-engine failure behavior. Check correlated placement-command state
before interpreting a missing PID during checksum verification; never increase the grace or
skip checksum verification to admit a judge.

If an interrupted unload leaves an owned evaluation member FAILED, cleanup requires a fresh
node-authenticated stop or exact-ID absence receipt before recording STOPPED and releasing its
sandbox hold. A FAILED database row alone is insufficient. Unreachable nodes, a still-stopping
process, or a response for another engine leave cleanup pending. Inspect the correlated unload
command and node reachability; do not edit rows or release holds manually.

Checkpoint candidate placement uses a scheduler-owned private instance bound to the
current evaluation attempt, trigger, training pause, fence and human owner. Ordinary
admin model selectors intentionally cannot serve evaluation-only adapters. If a
candidate reports `model_unavailable`, inspect those bindings and current operator
intent; do not make the adapter public or mark it verified.

Private adapter erasure first queries each node's authenticated import status. An
exact 404 means that node has no import to cancel; deletion still requires the exact
owned artifact and manifest receipt. Present imports must acknowledge verified or
cancelled state before deletion. Foreign, unreachable or transferring imports keep
ownership and checkpoint pins until cleanup can prove the stop and erasure.
