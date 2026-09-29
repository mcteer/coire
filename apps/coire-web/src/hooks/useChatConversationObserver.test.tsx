import { render, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useRef } from "react";
import { useChatConversationObserver } from "./useEventStream";

afterEach(() => vi.restoreAllMocks());

test("observes saved events with a scoped GET cursor and no generation POST", async () => {
  const conversationId = "00000000-0000-0000-0000-000000000001";
  const event = {
    conversation_id: conversationId,
    cursor: 4,
    turn_id: null,
    created_at: "2026-09-28T00:00:00Z",
    payload: {
      type: "turn.terminal",
      state: "stopped",
      answer_length: 7,
      reasoning_length: 0,
    },
  };
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(
      `event: turn.terminal\nid: ${conversationId}:4\ndata: ${JSON.stringify(event)}\n\n`,
      { headers: { "content-type": "text/event-stream" } },
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  const received = vi.fn();
  function Probe() {
    const cursor = useRef(3);
    useChatConversationObserver(conversationId, true, cursor, received);
    return null;
  }
  const view = render(<Probe />);
  await waitFor(() => expect(received).toHaveBeenCalledTimes(1), { timeout: 1500 });
  expect(fetchMock.mock.calls[0]?.[0]).toContain(`/conversations/${conversationId}/events`);
  expect(fetchMock.mock.calls[0]?.[1]?.method).toBeUndefined();
  expect(fetchMock.mock.calls[0]?.[1]?.headers).toEqual({
    "Last-Event-ID": `${conversationId}:3`,
  });
  view.unmount();
});
