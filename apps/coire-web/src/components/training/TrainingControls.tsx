import { useState } from "react";
import { controlTraining, deleteTrainingJob, type TrainingJob } from "../../api/training";
import { terminalTrainingState } from "../../hooks/useTrainingJob";
import { ConfirmAction } from "../ConfirmAction";
export function TrainingControls({ job, onChange }: { job: TrainingJob; onChange: () => Promise<void> }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [pending, setPending] = useState("");
  const action = async (operation: "pause" | "resume" | "cancel" | "delete") => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      if (operation === "delete") await deleteTrainingJob(job, crypto.randomUUID());
      else await controlTraining(job, operation, crypto.randomUUID());
      setPending(operation === "pause" ? "Pause requested: waiting for a mirrored checkpoint and confirmed stop (60 s deadline); forced-stop fallback reports the last durable step." : operation === "cancel" ? "Stop requested: termination is not yet confirmed. Healthy-node deadline is 5 s; uncertain reservations remain held." : operation === "resume" ? "Resume requested: current input, runtime and capacity checks must pass." : "Deletion requested: referenced lineage remains retained.");
      await onChange();
    } catch (e) { setError(String(e)); await onChange(); } finally { setBusy(false); }
  };
  return <section aria-label="Training controls">
    <div className="row">
      {job.state === "running" && <button className="button ghost" disabled={busy} onClick={() => void action("pause")}>Pause</button>}
      {job.state === "paused" && <button className="button ghost" disabled={busy} onClick={() => void action("resume")}>Resume</button>}
      {!terminalTrainingState(job.state) && job.state !== "cancelling" && <ConfirmAction label="Stop" target={job.spec.output.adapter_slug} onConfirm={() => action("cancel")}/>}
      {terminalTrainingState(job.state) && <ConfirmAction label="Delete job" target={job.id} onConfirm={() => action("delete")}/>}
    </div>
    {pending && !terminalTrainingState(job.state) && <p role="status">{pending}</p>}
    {job.state === "paused" && <p>{job.reason === "admin_pause" ? "Administrator pause: explicit Resume is required." : "Protective pause: automatic resume requires cooldown and fresh, valid admission evidence."}</p>}
    {["recovering", "cancelling", "pausing"].includes(job.state) && <p>Process liveness or checkpoint commitment is not yet confirmed. Capacity stays reserved until stop / fencing is proved.</p>}
    {error && <p role="alert" className="error">{error}</p>}
  </section>;
}
