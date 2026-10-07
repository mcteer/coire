import { useState } from "react";
import { promoteCheckpoint, type Checkpoint, type TrainingJob } from "../../api/training";
export function Checkpoints({ job, checkpoints, onChange }: { job: TrainingJob; checkpoints: Checkpoint[]; onChange: () => Promise<void> }) {
  const [selected, setSelected] = useState<string | null>(null), [slug, setSlug] = useState(""), [error, setError] = useState(""), [busy, setBusy] = useState(false);
  return <section aria-label="Checkpoints"><h3>Checkpoints</h3>
    {!checkpoints.length && <p>No durable checkpoint yet. Recovery before the first checkpoint restarts explicitly at update zero.</p>}
    <div className="training-checkpoints">{checkpoints.map((checkpoint) => <button type="button" className="button ghost" key={checkpoint.id} aria-pressed={selected === checkpoint.id} onClick={() => setSelected(checkpoint.id)}>Update {checkpoint.update} · {checkpoint.state}{job.latest_checkpoint_id === checkpoint.id ? " · latest" : ""}</button>)}</div>
    {checkpoints.filter((c) => c.id === selected).map((checkpoint) => <div className="training-card" key={checkpoint.id}>
      <p className="mono">Attempt {checkpoint.attempt_id} · fence {checkpoint.fence}<br/>{checkpoint.manifest_sha256}</p>
      <p>Verified copies: {checkpoint.verified_nodes?.join(" · ") || "None"}</p>
      {checkpoint.state === "committed" && checkpoint.verified_nodes?.length === 2 ? <form className="training-fields" onSubmit={(e) => {
        e.preventDefault(); setBusy(true); setError("");
        void promoteCheckpoint(checkpoint, { expected_version: job.version, adapter_slug: slug }, crypto.randomUUID()).then(onChange).catch((e) => setError(`Promotion unavailable or refused: ${String(e)}`)).finally(() => setBusy(false));
      }}><label>New adapter slug<input required pattern="[a-z0-9][a-z0-9-]*" maxLength={80} value={slug} onChange={(e) => setSlug(e.target.value)}/></label><button className="button ghost" disabled={busy}>Promote retained checkpoint</button><p>Creates a distinct private, unverified adapter after validation and replication. It never changes the checkpoint or automatically publishes cancelled work.</p></form> : <p>This checkpoint is not complete and mirrored; promotion is unavailable.</p>}
    </div>)}
    {error && <p role="alert" className="error">{error}</p>}
  </section>;
}
