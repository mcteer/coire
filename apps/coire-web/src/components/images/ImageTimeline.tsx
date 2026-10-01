import type { ImageJob } from "../../api/images";
import { isTerminalImageState } from "../../hooks/useImageJob";

export function ImageTimeline({
  jobId,
  state,
  step,
  totalSteps,
  onStop,
  busy,
  error = null,
}: {
  jobId: string;
  state: ImageJob["state"] | null;
  step: number | null;
  totalSteps: number | null;
  onStop: () => void;
  busy: boolean;
  error?: string | null;
}) {
  const terminal = isTerminalImageState(state);
  const progress =
    step != null && totalSteps != null
      ? `step ${step} of ${totalSteps}`
      : step != null
        ? `step ${step}`
        : null;
  return (
    <section className="panel glass wide" aria-label="Image job progress">
      <h2>Progress</h2>
      <p role="status">
        <span className="mono">{jobId}</span>
        {" · "}
        {state ?? "waiting for the server"}
        {progress ? ` · ${progress}` : ""}
      </p>
      {step != null && totalSteps != null && (
        <progress value={step} max={totalSteps} aria-label="Image generation progress" />
      )}
      <p>Cache status unavailable for this job.</p>
      <p>Worker residency status unavailable for this job.</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!terminal && (
        <button
          className="button"
          type="button"
          disabled={busy}
          onClick={onStop}
          aria-label={`Stop image job ${jobId}`}
        >
          {busy ? "Stopping…" : "Stop"}
        </button>
      )}
    </section>
  );
}
