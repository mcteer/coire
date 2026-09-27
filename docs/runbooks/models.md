# Runbook: the model registry

Peer replication uses only `coire-edge-a.fabric` and `coire-edge-b.fabric` on port 9401. It never
falls back to the control VLAN. See `network-fabrics.md` before changing connectivity.

Operating the roster: adding models, watching an acquisition, loading and unloading engines,
and clearing the states that need a human. Companion to `bootstrap.md`, which covers the
control plane itself.

Everything here uses `scripts/coire`, a `curl`+`jq` wrapper over the admin API. It reads the
admin bearer from `COIRE_ADMIN` or from core's Keychain item `coire-admin-token`.

```bash
export COIRE_API=http://127.0.0.1:8180        # the default host port
scripts/coire health                           # is the control plane up
scripts/coire nodes | jq '.[] | {name, reachability}'
```

## Adding a model

```bash
scripts/coire model add mlx-community/Qwen3.8-27B-8bit \
  '{"tags":["coding","reasoning"],"placement_policy":"single:auto"}'
```

Returns `202` and a registry row in `downloading`. Watch it:

```bash
ID=$(scripts/coire model list | jq -r '.[] | select(.repo_id=="mlx-community/Qwen3.8-27B-8bit") | .id')
watch -n5 "scripts/coire model job $ID | jq '{stage, percent, bytes_done, origin_node, replica_node}'"
```

The job walks `pull → verify_origin → export → import → verify_replica → done`. The model is
`ready` only when **both** Studios hold a copy whose every file matches the manifest. There is
no state in which one copy is enough.

### If the add is refused

A refusal happens before any bytes move and names the reason:

| Reason | What it means | What to do |
|---|---|---|
| `not_mlx_format` | the repository needs conversion | wait for feature 002, or find a pre-converted `mlx-community` build |
| `gated` | the licence has not been accepted | accept it on huggingface.co with the account whose token the Studios hold, then add again |
| `not_found` | no such repository | check the id; private repositories also read as not-found |
| `no_fit_memory` | it fits neither Studio | sharded placement is feature 006 |
| `no_fit_disk` | at least one Studio lacks room | the response quotes required and available bytes; retire something or add disk |

### If the acquisition fails

`scripts/coire model show $ID | jq '{state, state_reason, copies}'` says why. A checksum
mismatch lists the offending files on the copy that failed, and that partial copy has already
been deleted — a directory that failed verification is evidence, not a starting point.

```bash
scripts/coire model retry $ID     # resumes from the earliest incomplete stage
```

Retry keeps a verified origin copy and re-runs only what is missing. A pull resumes at file
granularity: files that completed are not fetched again.

If a node was simply down, the job holds at its stage with `state_reason` naming the node and
resumes on its own when the node returns — no retry needed.

If a Studio was upgraded in place while its LaunchDaemon was still running, restart the agent
before retrying. A stale in-memory supervisor can reject fields added to the job state schema and
surface as `not_found: no such job`; this does not mean the repository disappeared and no model
bytes need to be discarded. Run the restart interactively on that Studio, then confirm the node
is healthy and retry the failed model through the API:

```bash
sudo launchctl kickstart -k system/com.coire.node
scripts/coire nodes | jq '.[] | {name, reachability: .reachability}'
scripts/coire model retry "$ID"
```

## Curating

```bash
scripts/coire model update $ID '{"visibility":"published"}'      # appears in the picker
scripts/coire model update $ID '{"visibility":"admin_only"}'     # disappears; nothing unloads
scripts/coire model update $ID '{"tags":["coding"],"description":"good at refactors"}'
scripts/coire model update $ID '{"chat_template":"{{ ... }}"}'   # applies on the NEXT load
scripts/coire model update $ID '{"chat_template":null}'          # back to the repo's own
```

Publishing requires `ready`. Unpublishing is immediate and touches nothing else — no unload,
no deletion. A chat-template change does not disturb a running engine; each engine reports the
digest of the template it started with (`chat_template_sha256`).

## Retiring and deleting

```bash
scripts/coire model retire $ID    # unload everywhere, delete both copies, keep the row
scripts/coire model delete $ID    # only for a failed model: removes the row entirely
```

Retirement is driven to completion by the reconciler: if a Studio is down when you retire, the
model still moves to `retired` and the deletion is retried until that node confirms. The row is
kept for audit either way.

## Loading and unloading

```bash
scripts/coire model load $ID                      # placement policy chooses the node
scripts/coire model load $ID coire-edge-b         # override
scripts/coire engines | jq '.[] | {id, node, state, resident_bytes, resident_delta_bytes}'
scripts/coire engine unload <engine-id>
```

A load returns `202` and `starting`. It becomes `ready` only when the node agent has issued a
generation request and got an answer — the engine's own `/health` answers well before the
weights are in memory (measured: 1.5 s early on a 0.5 GB model, minutes on a large one), so
"the port is open" is never treated as ready.

**`409 budget`** means the node refused: the response carries `required_bytes`,
`committed_bytes` and `budget_bytes`. Unload something, or wait for feature 004's eviction.

**A second load of the same model on the same node** returns `200` with the existing engine.
That is deliberate, not an error.

## States that need a human

### An orphan engine

An engine running on a Studio that the control plane did not start — usually a hand-started
process, or one left by an agent that was replaced. It is **reported, never adopted and never
killed**: adopting a process whose provenance is unknown would put unaccounted memory into the
budget, and killing one might destroy work a person is doing deliberately.

```bash
scripts/coire engines | jq '.[] | select(.state=="orphan")'
scripts/coire engine unload <id>          # stops it and clears the row
```

### An engine that says `failed`

`state_reason` distinguishes the cases: `the engine exited with status N during startup` comes
with `exit_output` (the engine's last 4 KiB of stderr); `the engine process exited` means it
died later; `process gone during agent restart` means it did not survive an agent restart it
should have — check that the LaunchDaemon plist still has `AbandonProcessGroup`.

### A model that is `replicating` for a long time

Normal while a large copy crosses the mesh. `scripts/coire model job $ID` shows byte progress.
If `bytes_done` is not advancing, check the mesh link between the two Studios: replication
never falls back to Wi-Fi by design, so a broken mesh stalls it rather than slowing it.

## Where things live on a Studio

| Path | What | Safe to delete by hand? |
|---|---|---|
| `/opt/coire/models/<slug>/` | the model's files | only via `model retire`; deleting by hand leaves the registry wrong until the next reconcile |
| `/opt/coire/models/<slug>.manifest.json` | the checksum manifest | no — verification needs it |
| `/opt/coire/models/<slug>.chat_template.jinja` | template override, if any | harmless; regenerated on next load |
| `/opt/coire/state/engines.json` | which engines this agent owns | **no**, not while the agent runs — this is how it finds them after a restart |
| `/opt/coire/state/jobs/*.json` | in-flight acquisition state | **no**, not while a job runs |
| `/opt/coire/hf-cache/` | Hugging Face metadata scratch | yes; it is re-fetched |

These files are **caches, not truth**. If the registry and a node disagree, the reconcile
result wins and the discrepancy is audited. Never edit them to "fix" state — retry or retire
through the API instead.

## Rotating credentials

```bash
# admin token (core)
security delete-generic-password -s coire-admin-token
scripts/coire-secrets-init.sh            # recreates the missing one
deploy/compose/coire-up                  # restart so the new value is mounted

# Hugging Face token (each Studio, System keychain — never on core)
sudo security delete-generic-password -s coire-hf-token /Library/Keychains/System.keychain
sudo security add-generic-password -a coire -s coire-hf-token \
     -w '<hf_...>' /Library/Keychains/System.keychain
sudo launchctl kickstart -k system/com.coire.node
```

The Hugging Face token exists only on the Studios. If you find one anywhere on core — in a
container's environment, in `/run/secrets`, in the repository — that is a bug; the integration
suite checks all three.

## Reading the audit log

Every administrative mutation and every refusal writes a row.

```bash
scripts/coire audit 50 | jq '.[] | {at, actor, action, target_id, outcome}'
scripts/coire audit | jq '.[] | select(.outcome=="refused")'
```

Until feature 007 the actor is the literal `admin-token` for admin actions and `anonymous` for
refusals. That is the truthful record of this period and is not rewritten later. Nothing in
the application deletes or edits an audit row.
