import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useImageJobEvents } from "./useImageJobEvents";

const JOB = "01J00000000000000000000000";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function Probe() {
  const stream = useImageJobEvents(JOB);
  return (
    <output>
      {stream.error ?? (stream.data ? `${stream.data.type}:${stream.data.sequence}` : "")}
    </output>
  );
}

function frame(sequence: number, type: string): string {
  return `id: ${JOB}:${sequence}\nevent: ${type}\ndata: ${JSON.stringify({
    job_id: JOB,
    sequence,
    at: "2026-09-30T00:00:00Z",
    type,
    state: type === "error" ? "failed" : "queued",
    ...(type === "error" ? { safe_code: "worker_unavailable" } : {}),
  })}\n\n`;
}

test("ignores duplicate image frames and stops reconnecting after a terminal event", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode(frame(1, "queued") + frame(1, "queued") + frame(2, "error")),
      );
      controller.close();
    },
  });
  const fetchMock = vi
    .fn()
    .mockResolvedValue(new Response(body, { headers: { "content-type": "text/event-stream" } }));
  vi.stubGlobal("fetch", fetchMock);
  render(<Probe />);
  await waitFor(() => expect(screen.getByText("error:2")).toBeInTheDocument());
  await new Promise((resolve) => setTimeout(resolve, 700));
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("rejects a missing image event sequence", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(frame(1, "queued") + frame(3, "error")));
      controller.close();
    },
  });
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(new Response(body, { headers: { "content-type": "text/event-stream" } })),
  );
  render(<Probe />);
  await waitFor(() => expect(screen.getByText(/image event gap/)).toBeInTheDocument());
});

test("stops retrying after image event access is revoked", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 403 }));
  vi.stubGlobal("fetch", fetchMock);
  render(<Probe />);
  await waitFor(() => expect(screen.getByText(/stream refused \(403\)/)).toBeInTheDocument());
  await new Promise((resolve) => setTimeout(resolve, 700));
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
