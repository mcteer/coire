import type { ImageJob } from "../api/images";
import { useImageJobEvents } from "./useImageJobEvents";

const TERMINAL_STATES = new Set<ImageJob["state"]>(["succeeded", "failed", "cancelled"]);

export function isTerminalImageState(state: ImageJob["state"] | null): boolean {
  return state !== null && TERMINAL_STATES.has(state);
}

/** One owned job, observed through the existing image event stream. */
export function useImageJob(jobId: string | null) {
  const stream = useImageJobEvents(jobId);
  const event = stream.data?.job_id === jobId ? stream.data : null;
  const snapshot = event?.snapshot;
  const state = snapshot?.state ?? event?.state ?? null;
  const step = snapshot?.progress_step ?? event?.step ?? null;
  const total = event?.total_steps ?? null;
  const failureCode = snapshot?.failure_code ?? event?.safe_code ?? null;
  return {
    observedJobId: event?.job_id ?? null,
    state,
    step,
    total,
    terminal: isTerminalImageState(state),
    error: stream.error ?? failureCode,
  };
}
