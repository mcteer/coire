import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useEventStream } from "./useEventStream";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function Probe() {
  const stream = useEventStream<{ cursor: string }>("/events");
  return <output>{stream.data?.cursor ?? stream.error ?? "connecting"}</output>;
}

function response(cursor: string): Response {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode(
          `id: ${cursor}\nevent: snapshot\ndata: {"snapshot":{"cursor":"${cursor}"}}\n\n`,
        ),
      );
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

test("reconnects with Last-Event-ID and reconciles new state", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(response("10"))
    .mockResolvedValueOnce(response("11"));
  vi.stubGlobal("fetch", fetchMock);
  render(<Probe />);
  await waitFor(() => expect(screen.getByText("10")).toBeInTheDocument());
  await waitFor(
    () => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[1]?.[1]?.headers).toEqual({ "Last-Event-ID": "10" });
      expect(screen.getByText("11")).toBeInTheDocument();
    },
    { timeout: 2500 },
  );
});
