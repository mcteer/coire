import { expect, test, vi } from "vitest";
import { streamFailoverCompletion } from "./failover";

test("streams CRLF events and the final frame without a blank line", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(
      'data: {"choices":[{"delta":{"content":"one"}}]}\r\n\r\ndata: {"choices":[{"delta":{"content":"two"},"finish_reason":"stop"}]}',
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ),
  );
  const chunks: string[] = [];
  await streamFailoverCompletion(
    "11111111-1111-1111-1111-111111111111",
    [{ role: "user", content: "hello" }],
    (chunk) => chunks.push(chunk),
  );
  expect(chunks).toEqual(["one", "two"]);
  fetch.mockRestore();
});

test("authority interruption is not reported as a completed answer", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(
    'event: error\ndata: {"reason":"not_elected","coire_interrupted":true}\n\n',
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  ));
  await expect(streamFailoverCompletion("fixture", [{ role: "user", content: "hello" }], () => {}))
    .rejects.toThrow(/interrupted/i);
  fetch.mockRestore();
});

test("a stream ending without a terminal frame is interrupted", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(
    'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  ));
  await expect(streamFailoverCompletion("fixture", [{ role: "user", content: "hello" }], () => {}))
    .rejects.toThrow(/interrupted/i);
  fetch.mockRestore();
});
