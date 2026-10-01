import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { isTerminalImageState, useImageJob } from "./useImageJob";

const JOB = "01J00000000000000000000000";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function Probe({ jobId }: { jobId: string | null }) {
  const job = useImageJob(jobId);
  return (
    <output>
      {job.error ??
        `${job.state ?? "idle"}:${job.step ?? ""}:${job.total ?? ""}:${job.terminal ? "terminal" : "open"}`}
    </output>
  );
}

function frame(sequence: number, type: string, extra: Record<string, unknown> = {}): string {
  const state =
    type === "error"
      ? "failed"
      : type === "done"
        ? "succeeded"
        : type === "cancelled"
          ? "cancelled"
          : type === "progress" || type === "started"
            ? "running"
            : "queued";
  return `id: ${JOB}:${sequence}\nevent: ${type}\ndata: ${JSON.stringify({
    job_id: JOB,
    sequence,
    at: "2026-09-30T00:00:00Z",
    type,
    state,
    ...extra,
  })}\n\n`;
}

test("does not open an event stream without a job", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  render(<Probe jobId={null} />);
  expect(screen.getByText("idle:::open")).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("treats succeeded, failed, and cancelled as terminal", () => {
  expect(isTerminalImageState("succeeded")).toBe(true);
  expect(isTerminalImageState("failed")).toBe(true);
  expect(isTerminalImageState("cancelled")).toBe(true);
  expect(isTerminalImageState("queued")).toBe(false);
  expect(isTerminalImageState("running")).toBe(false);
  expect(isTerminalImageState(null)).toBe(false);
});

test("prefers the server snapshot over the event envelope", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode(
          frame(1, "reset", {
            state: "queued",
            total_steps: 8,
            step: 1,
            snapshot: {
              id: JOB,
              state: "running",
              latest_event_sequence: 1,
              progress_step: 3,
            },
          }),
        ),
      );
    },
  });
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(new Response(body, { headers: { "content-type": "text/event-stream" } })),
  );
  render(<Probe jobId={JOB} />);
  await waitFor(() => expect(screen.getByText("running:3:8:open")).toBeInTheDocument());
});

test("records a terminal result from the event stream", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(frame(1, "done", { step: 8, total_steps: 8 })));
      controller.close();
    },
  });
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(new Response(body, { headers: { "content-type": "text/event-stream" } })),
  );
  render(<Probe jobId={JOB} />);
  await waitFor(() => expect(screen.getByText("succeeded:8:8:terminal")).toBeInTheDocument());
});

test("shows a server-safe failure code for a failed image job", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode(frame(1, "error", { safe_code: "model_unavailable" })),
      );
      controller.close();
    },
  });
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(new Response(body, { headers: { "content-type": "text/event-stream" } })),
  );
  render(<Probe jobId={JOB} />);
  await waitFor(() => expect(screen.getByText("model_unavailable")).toBeInTheDocument());
});

test("surfaces a stream error without treating it as a terminal job", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 403 })));
  render(<Probe jobId={JOB} />);
  await waitFor(() => expect(screen.getByText(/stream refused \(403\)/)).toBeInTheDocument());
});
