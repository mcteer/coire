import { useEffect, useState } from "react";
import {
  controlTraining,
  listTrainingActivity,
  type TrainingActivityItem,
} from "../../api/training";
import { ConfirmAction } from "../ConfirmAction";
export function TrainingActivity() {
  const [jobs, setJobs] = useState<TrainingActivityItem[]>([]),
    [error, setError] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    try {
      const page = await listTrainingActivity();
      setJobs(page.items);
      setNextCursor(page.next_cursor ?? null);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => {
    void refresh();
  }, []);
  const stop = async (job: TrainingActivityItem) => {
    setBusy(true);
    try {
      await controlTraining(
        { id: job.job_id, version: job.version },
        "cancel",
        crypto.randomUUID(),
      );
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const older = async () => {
    if (!nextCursor || busy) return;
    setBusy(true);
    try {
      const page = await listTrainingActivity(nextCursor);
      setJobs((current) => [
        ...new Map([...current, ...page.items].map((job) => [job.job_id, job])).values(),
      ]);
      setNextCursor(page.next_cursor ?? null);
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-label="Training activity">
      <h3>Training jobs</h3>
      {error && <p role="alert">Training activity unavailable: {error}</p>}
      {jobs.length === 0 ? (
        <p>No training jobs.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>State / reason</th>
              <th>Completed updates</th>
              <th>Loss (train / validation)</th>
              <th>Reserved memory</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.job_id}>
                <td>
                  <a href={`#training/run/${job.job_id}`}>{job.adapter_slug}</a>
                </td>
                <td>
                  {job.state} · {job.safe_reason?.replaceAll("_", " ") ?? "no pending reason"}
                </td>
                <td className="mono">
                  {job.completed_update} / {job.total_updates}
                </td>
                <td className="mono">
                  {job.latest_train_loss?.toFixed(4) ?? "—"} /{" "}
                  {job.latest_validation_loss?.toFixed(4) ?? "—"}
                </td>
                <td className="mono">{(job.reserved_bytes / 1024 ** 3).toFixed(2)} GiB</td>
                <td>
                  {job.can_stop && !busy && (
                    <ConfirmAction
                      target={job.adapter_slug}
                      label="Stop"
                      onConfirm={() => stop(job)}
                    />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {nextCursor && (
        <button type="button" className="button" disabled={busy} onClick={() => void older()}>
          Load older training jobs
        </button>
      )}
    </section>
  );
}
