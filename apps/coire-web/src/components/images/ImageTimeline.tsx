import { useEffect, useRef } from "react";
import type { ImageJob, ImageJobEvent } from "../../api/images";
import { isTerminalImageState } from "../../hooks/useImageJob";

export function ImageTimeline({
  jobId,
  state,
  step,
  totalSteps,
  cacheStatus = null,
  workerResidency = null,
  onStop,
  busy,
  error = null,
  queuePosition = null,
  onViewResult,
}: {
  jobId: string;
  state: ImageJob["state"] | null;
  step: number | null;
  totalSteps: number | null;
  cacheStatus?: ImageJobEvent["cache_status"];
  workerResidency?: ImageJobEvent["worker_residency"];
  onStop: () => void;
  busy: boolean;
  error?: string | null;
  queuePosition?: number | null;
  onViewResult?: () => void;
}) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    panel.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [jobId]);
  const terminal = isTerminalImageState(state);
  const progress =
    step != null && totalSteps != null
      ? `step ${step} of ${totalSteps}`
      : step != null
        ? `step ${step}`
        : null;
  const messages: Record<NonNullable<typeof state>, [string, string]> = {
    queued: [
      "Queued",
      queuePosition != null
        ? `Position ${queuePosition} in the queue. Your request has been accepted.`
        : "Your request has been accepted and is waiting for a worker.",
    ],
    reserving: [
      "Preparing generation",
      "Allocating space and preparing the image worker. A cold model can take longer to start.",
    ],
    running: [
      "Generating your image",
      "The worker is generating your image. Step updates will appear when available.",
    ],
    transferring: [
      "Saving your image",
      "Generation is finishing. The image is being saved to your private library.",
    ],
    cancelling: [
      "Stopping generation",
      "Cancellation was requested. Waiting for the worker to confirm.",
    ],
    succeeded: ["Image ready", "Your completed image is available in the library."],
    failed: [
      "Generation failed",
      "This request could not finish. Review the error below before trying again.",
    ],
    cancelled: ["Generation stopped", "This request was cancelled."],
  };
  const [title, description] = state
    ? messages[state]
    : ["Request accepted", "Connecting to live progress…"];
  return (
    <section ref={panel} className="image-timeline" aria-label="Image job progress">
      <p className="image-progress-title" role="status">
        {!terminal && <span className="image-progress-indicator" aria-hidden="true" />}
        {title}
        {progress ? ` · ${progress}` : ""}
      </p>
      <p>{description}</p>
      {step != null && totalSteps != null && (
        <progress value={step} max={totalSteps} aria-label="Image generation progress" />
      )}
      <details>
        <summary>Job details</summary>
        <p className="mono">
          {jobId} · {state ?? "connecting"}
        </p>
        <p>
          {cacheStatus === "hit"
            ? "Prompt cache reused."
            : cacheStatus === "cold"
              ? "Prompt cache cold: encoding ran."
              : cacheStatus === "evicted"
                ? "Prompt cache evicted: encoding ran again."
                : "Prompt cache status unavailable for this job."}
        </p>
        <p>
          {workerResidency === "resident"
            ? "Worker observed resident at the last progress update."
            : "Worker residency status unavailable for this job."}
        </p>
      </details>
      {error && (
        <p className="error" role="alert">
          {terminal
            ? error
            : `Reconnecting to live progress. Your submitted job may still be running. ${error}`}
        </p>
      )}
      {state === "succeeded" && onViewResult && (
        <button className="button" type="button" onClick={onViewResult}>
          View in library
        </button>
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
