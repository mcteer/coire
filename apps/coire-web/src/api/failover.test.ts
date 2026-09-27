import { expect, test, vi } from "vitest";
import { streamFailoverCompletion } from "./failover";

test("streams CRLF events and the final frame without a blank line", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(
      'data: {"choices":[{"delta":{"content":"one"}}]}\r\n\r\ndata: {"choices":[{"delta":{"content":"two"}}]}',
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
