import { useRef } from "react";
import { imageJobEventsUrl, type ImageJobEvent } from "../api/images";
import { useEventStream } from "./useEventStream";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);
const EVENT_TYPES = new Set([
  "queued",
  "started",
  "progress",
  "done",
  "error",
  "cancelled",
  "reset",
]);

/** Observe one owned job through the shared SSE transport, ignoring duplicate frames. */
export function useImageJobEvents(jobId: string | null) {
  const cursor = useRef({ jobId, sequence: 0 });
  if (cursor.current.jobId !== jobId) cursor.current = { jobId, sequence: 0 };

  return useEventStream<ImageJobEvent>(jobId ? imageJobEventsUrl(jobId) : "", null, {
    decode(frame) {
      const value: unknown = JSON.parse(frame.data);
      if (!value || typeof value !== "object") throw new Error("invalid image event");
      const event = value as ImageJobEvent;
      if (
        event.job_id !== jobId ||
        !Number.isSafeInteger(event.sequence) ||
        event.sequence < 1 ||
        !EVENT_TYPES.has(event.type) ||
        frame.id !== `${jobId}:${event.sequence}` ||
        frame.event !== event.type ||
        (event.type === "reset" &&
          (!event.snapshot ||
            event.snapshot.id !== jobId ||
            event.snapshot.latest_event_sequence !== event.sequence))
      )
        throw new Error("invalid image event");
      if (event.sequence <= cursor.current.sequence) return undefined;
      if (event.type !== "reset" && event.sequence !== cursor.current.sequence + 1)
        throw new Error("image event gap");
      cursor.current.sequence = event.sequence;
      return event;
    },
    isTerminal(event) {
      return (
        event.type === "done" ||
        event.type === "error" ||
        event.type === "cancelled" ||
        (event.type === "reset" && !!event.snapshot && TERMINAL_STATES.has(event.snapshot.state))
      );
    },
  });
}
