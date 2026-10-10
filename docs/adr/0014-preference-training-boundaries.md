# ADR-0014: Preference objectives through bare MLX trainer hooks

Date: 2026-10-08. Status: accepted for implementation in spec 018.

## Context

Training 016 already owns native MLX process lifecycle, full checkpoints and guarded admission. The roadmap proposed mlx-lm-lora or an in-house fallback for preference objectives. A second training stack would require a new dependency and independent recovery qualification.

## Decision

Use the pinned MLX 0.32.2 / mlx-lm 0.31.3 trainer's public loss, iterator and callback hooks with small Coire DPO/ORPO adapters. Do not fork the upstream optimization loop or add a dependency. DPO freezes the exact initial policy (base plus initial adapter); ORPO is reference-free. Pair-averaged objective counts and actual response-token throughput remain separate. Numerical probes run outside compiled gradient loss and preserve RNG/sampler state.

Preserve immutable SFT v1/v2 documents with separate preference v3 intent/resolved/checkpoint identities. Start with measured single-Studio dense LoRA and affine 4-bit/group-64 QLoRA, zero dropout, existing approved architectures; refuse DoRA and distributed preference training. Initial artifacts are registry-bound, locally acquired, mirrored and pinned. New results carry independent verification and complete standalone adapter tensors.

## Consequences and qualification

No new production image/service/network or engine port is required. Coire-node owns model/tokenizer/training work; core persists and orchestrates only (Principles I, II, III and V). The new loss/mask, frozen-reference, initialization/RNG and count semantics require independent numerical and full-process recovery tests. Objective-specific memory/coexistence evidence is mandatory; SFT profiles do not qualify preference jobs. Per-surface contracts, telemetry/alerts/runbooks, tiny-model CI and real-Studio qualification remain release gates (IV, VI and VII).

Sources: [pinned bare trainer](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/mlx_lm/tuner/trainer.py), [DPO authors](https://github.com/eric-mitchell/direct-preference-optimization/blob/main/trainers.py), [ORPO paper](https://arxiv.org/html/2403.07691v2). This ADR authorizes no unsupported runtime combination or relaxed gate.

Measured chat requests execute in the actual gateway API process. The scheduler
uses the existing configured training API URL and node credentials to call a
strict internal measurement endpoint. The endpoint accepts only target/prompt
values bound to an already-running measurement and a digest of its frozen owner
principal; it reconstructs that principal from the command, checks node membership,
and performs fresh owner/key, registry, exact-instance/engine/artifact and hold
checks. It returns content-free completion metadata. Route work is included in
gateway first-token/overhead timing, and normal disconnect/credential checks
remain active. The scheduler owns workload arrival and training orchestration;
placing those arrivals in the scheduler's own proxy event loop contaminated the
gateway performance measurement with unrelated control-plane work. No new service,
credential, inference wrapper or network permission is introduced.

Gateway qualification also requires non-blocking credential verification. Existing
Argon2id verification keeps its exact parameters and one-hash memory bound, but
runs in the existing AnyIO worker pool so it cannot stall unrelated gateway
streams. A fresh credential snapshot comparison and active-user read after the
await prevent revocation, rotation or owner changes from granting old authority.
No verification result is cached and all downstream live action checks remain.


Measurement gateway admission reuses the internal request transaction and fixed
bound SQL structures while continuing to read live authority and registry rows.
A bounded API-owned database pool lets the initial readonly lookup test liveness
without a separate ping. It retries once only after an invalidated connection
and transaction rollback, before any admission write or generation; subsequent
operations are never replayed. The ordinary database pool is unchanged. Node
UUID/name pairs are immutable lock/identity bindings checked against fresh
joined inventory on every guard. Quota/rate admission and lease/counter writes
remain atomic; no authority, query result or completed admission is cached.

Readonly private measurement authorization and watcher checks use PostgreSQL
FOR SHARE on owner/key rows. Concurrent readers can proceed, while deactivation,
revocation, rotation and scope removal wait until those checks commit. Mutation
paths retain exclusive locks. The watcher resolves all frozen residents and
counted model/training holds in one fresh inventory query under canonical node
locks, checking exact engine, registry, artifact and node identities. No live
authority or inventory result is cached.

Repeated private gateway checks compare PostgreSQL-computed SHA-256 over both
complete fresh stored request and command documents. This avoids transferring
and decoding unchanged frozen JSON. Changed digests reload and revalidate the
documents; any midstream change refuses generation. The digest does not replace
current owner/key, registry, artifact, node or counted-hold checks.

The API explicitly selects Uvicorn's uvloop event loop to reduce I/O scheduling
overhead while preserving the 20 ms gateway p95 requirement. `uvloop==0.22.1`
is exactly pinned with distribution hashes; its MIT / Apache-2.0 licences are
compatible. It runs no model work and adds no inference wrapper. Native node
training environments remain independent of this API-only runtime dependency.

Warm routing reuses only the immutable execution principal from validated
parsing. The callback's first fresh database operation still checks current
owner/key state under mutation locks and complete stored-document SHA-256 before
any admission write. Changed identities/documents refuse engine IO. Only that
initial readonly operation may reconnect once; no later operation is replayed.

The private gateway runs one ASGI disconnect watcher per request. Stream-frame
checks read its event; cancellation or a failed receive prevents output and
releases the watcher. Ordinary request disconnect handling is unchanged.

Private admission holds fresh authority locks through its atomic request-lease
commit. Its independent credential recheck starts at upstream handoff, overlaps
model work and must pass before forwarding the first chunk. Every later live
stream check remains fresh. Failure before handoff cancels the pending checker;
direct sources still recheck before output. Ordinary streams retain their early
credential check. This changes scheduling, never the authority or latency limit.


Private measurement admission combines the existing fresh-key rate/monthly-quota
upsert with its dependent hold, in-flight and request-lease writes in one database
round trip. The same owner/key and canonical node locks span authorization,
inventory, counters and commit. Refused counters produce no dependent writes;
error classification preserves rate-first precedence without repeating admission.
Refusals launch no inference and create no usage settlement. This batches work
without caching live limits or changing ordinary public request admission.

The measurement watchdog renews prepared reservations throughout input delivery
and baseline as well as running trainers. Both use the same 30-second lease and fresh authority,
binding and safe-resource checks. Node expiry and stopped-work rejection remain
unchanged; a prepared reservation must never gain an unbounded baseline lease.


Fresh retained-disk accounting indexes each currently observed artifact path under
its enclosing scopes once per calculation. Reservation claims consume disjoint
paths from that index, preserving symlink checks, fresh byte counts, unknown-file
charges and retained-artifact credits. This avoids history-sized repeated scans
under the node admission lock; it introduces no cached quota or history deletion.

Frozen principal hashing canonicalizes unordered scope, entitlement, model-ID
and tool sets before hashing across API and scheduler processes. Ordered fields
and non-principal training-spec digests retain their historical serialization.
The digest binds immutable execution identity; it never replaces fresh owner,
key, node, inventory and admission checks.
