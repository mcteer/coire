import { useCallback, useEffect, useRef, useState } from "react";
import { getTrainingJob, listCheckpoints, listTrainingMetrics, trainingEventsUrl, type Checkpoint, type TrainingEvent, type TrainingJob, type TrainingMetric } from "../api/training";
import { useEventStream } from "./useEventStream";

export const terminalTrainingState = (state: TrainingJob["state"]) => ["succeeded", "failed", "cancelled"].includes(state);
export function mergeTrainingMetrics(old: TrainingMetric[], incoming: TrainingMetric[]): TrainingMetric[] {
  const samples = new Map(old.map((m) => [`${m.attempt_id}:${m.update}:${m.kind}`, m]));
  for (const sample of incoming) samples.set(`${sample.attempt_id}:${sample.update}:${sample.kind}`, sample);
  return [...samples.values()].sort((a, b) => a.recorded_at.localeCompare(b.recorded_at) || a.update - b.update);
}

export function useTrainingJob(id: string | null) {
  const [job, setJob] = useState<TrainingJob | null>(null);
  const [metrics, setMetrics] = useState<TrainingMetric[]>([]);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const cursor = useRef(0);
  const refresh = useCallback(async () => {
    if (!id) return;
    const current = generation.current;
    try {
      const detail = await getTrainingJob(id);
      const allMetrics: TrainingMetric[] = [], allCheckpoints: Checkpoint[] = [];
      let next: string | null | undefined = null;
      do { const page = await listTrainingMetrics(id, next); allMetrics.push(...page.items); next = page.next_cursor; } while (next && current === generation.current);
      next = null;
      do { const page = await listCheckpoints(id, next); allCheckpoints.push(...page.items); next = page.next_cursor; } while (next && current === generation.current);
      if (current !== generation.current) return;
      setJob((old) => old && old.version > detail.version ? old : detail);
      setMetrics((old) => mergeTrainingMetrics(old, allMetrics)); setCheckpoints(allCheckpoints); setError("");
    } catch (e) { if (current === generation.current) setError(String(e)); }
  }, [id]);
  useEffect(() => {
    generation.current++; cursor.current = 0;
    setJob(null); setMetrics([]); setCheckpoints([]); setError("");
    void refresh();
    // Authoritative polling also observes control deadline fallback during a broken SSE connection.
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => { generation.current++; window.clearInterval(timer); };
  }, [refresh]);
  const applyEvent = (event: TrainingEvent) => {
    const payload = event.payload;
    if (payload.kind === "reset") {
      setJob((old) => old && payload.snapshot.version >= old.version ? { ...old, ...payload.snapshot } : old);
      setMetrics([]); void refresh();
    } else if (payload.kind === "progress" || payload.kind === "preference_progress") {
      setMetrics((old) => mergeTrainingMetrics(old, [payload.metric]));
      setJob((old) => old && event.state_version >= old.version ? { ...old, completed_update: payload.metric.update, attempt_id: event.attempt_id, version: event.state_version } : old);
    } else {
      if (payload.kind === "state" || payload.kind === "terminal") setJob((old) => old && event.state_version >= old.version ? { ...old, state: payload.state, reason: payload.reason, version: event.state_version } : old);
      void refresh();
    }
  };
  const stream = useEventStream<TrainingEvent>(id && !terminalTrainingState(job?.state ?? "queued") ? trainingEventsUrl(id) : "", null, {
    decode: (frame) => {
      const event = JSON.parse(frame.data) as TrainingEvent;
      if (event.job_id !== id || !Number.isSafeInteger(event.id) || frame.id !== String(event.id) || frame.event !== event.kind || event.payload?.kind !== event.kind) throw new Error("Invalid training event identity");
      if (event.id <= cursor.current && event.kind !== "reset") return undefined;
      if (event.kind !== "reset" && event.id !== cursor.current + 1) { void refresh(); throw new Error("Training event gap; refreshing persisted history"); }
      cursor.current = event.id;
      // Apply every frame, including multiple progress frames in one browser render.
      applyEvent(event);
      return event;
    },
  });
  return { job, metrics, checkpoints, error, connected: stream.connected, streamError: stream.error, refresh };
}
