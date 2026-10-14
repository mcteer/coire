import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useEventStream } from "./useEventStream";
import { useChatConversationObserver } from "./useEventStream";
import { useRef, useState } from "react";
import type { ChatEvent } from "../api/chat";

const originalSetTimeout = window.setTimeout;
const originalClearTimeout = window.clearTimeout;
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  // The timeout spy captures fake timers. Restore the original browser timers
  // after restoring spies so later tests cannot inherit that fake clock.
  window.setTimeout = originalSetTimeout;
  window.clearTimeout = originalClearTimeout;
});

function Probe() {
  const stream = useEventStream<{ cursor: string }>("/events");
  return <output>{stream.data?.cursor ?? stream.error ?? "connecting"}</output>;
}

function PrivacyProbe() {
  const cursor = useRef(5);
  const [status, setStatus] = useState("candidate visible");
  useChatConversationObserver("conversation", true, cursor, (event: ChatEvent) => {
    if (event.payload.type === "snapshot") setStatus("withdrawn snapshot received");
  });
  return <output>{status}</output>;
}

test("accepts a privacy replacement snapshot at the current cursor", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode(
          'id: conversation:5\nevent: snapshot\ndata: {"conversation_id":"conversation","cursor":5,"payload":{"type":"snapshot","replacement":true,"detail":{}}}\n\n',
        ),
      );
    },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    ),
  );
  render(<PrivacyProbe />);
  expect(await screen.findByText("withdrawn snapshot received")).toBeInTheDocument();
});

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

test("aborts hidden streams and resumes with the cursor when visible", async () => {
  let visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(
    () => visibility as DocumentVisibilityState,
  );
  const signals: AbortSignal[] = [];
  const fetchMock = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
    const signal = init.signal as AbortSignal;
    signals.push(signal);
    let bodyController: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        bodyController = controller;
        controller.enqueue(
          new TextEncoder().encode(
            'id: 10\nevent: snapshot\ndata: {"snapshot":{"cursor":"10"}}\n\n',
          ),
        );
      },
    });
    signal.addEventListener("abort", () => bodyController.close(), { once: true });
    return Promise.resolve(new Response(body, { status: 200 }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const view = render(<Probe />);
  await waitFor(() => expect(screen.getByText("10")).toBeInTheDocument());
  await act(async () => {
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
  });
  assertAborted(signals[0]);
  await act(async () => {
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(fetchMock.mock.calls[1]?.[1]?.headers).toEqual({ "Last-Event-ID": "10" });
  vi.spyOn(navigator, "onLine", "get")
    .mockReturnValueOnce(false)
    .mockReturnValueOnce(false)
    .mockReturnValue(true);
  await act(async () => window.dispatchEvent(new Event("offline")));
  assertAborted(signals[1]);
  await act(async () => window.dispatchEvent(new Event("online")));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  view.unmount();
  assertAborted(signals[2]);
});

function assertAborted(signal: AbortSignal | undefined) {
  expect(signal?.aborted).toBe(true);
}

test("outage retry jitter never exceeds the ten-second replay budget", async () => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(1);
  const timers = vi.spyOn(window, "setTimeout");
  const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
  vi.stubGlobal("fetch", fetchMock);
  const view = render(<Probe />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  const delays = timers.mock.calls.map((call) => call[1] ?? 0);
  expect(fetchMock.mock.calls.length).toBeGreaterThan(5);
  expect(delays).toContain(10_000);
  expect(delays.every((delay) => delay <= 10_000)).toBe(true);
  view.unmount();
});

test.each([401, 403])(
  "denied stream %i stays stopped across visibility and online events",
  async (status) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status }));
    vi.stubGlobal("fetch", fetchMock);
    function AccessProbe() {
      const stream = useEventStream("/private-events");
      return <output>{stream.accessDenied ? "access denied" : "connecting"}</output>;
    }
    render(<AccessProbe />);
    expect(await screen.findByText("access denied")).toBeInTheDocument();
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("online"));
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  },
);
