import { afterEach, expect, test, vi } from "vitest";
import {
  getChatConversation,
  listChatConversations,
  sendChatTurn,
  type ChatTurnCreate,
} from "./chat";

afterEach(() => vi.restoreAllMocks());

const conversationId = "00000000-0000-0000-0000-000000000001";
const body: ChatTurnCreate = {
  client_request_id: "00000000-0000-0000-0000-000000000002",
  expected_revision: 1,
  model_id: "00000000-0000-0000-0000-000000000003",
  content: "Hi",
  action: "chat",
};

function streamResponse(chunks: string[]): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
        controller.close();
      },
    }),
    { headers: { "content-type": "text/event-stream; charset=utf-8" } },
  );
}

function terminal(cursor: number, conversation = conversationId): string {
  return `event: turn.terminal\nid: ${conversation}:${cursor}\ndata: ${JSON.stringify({
    conversation_id: conversation,
    cursor,
    turn_id: body.client_request_id,
    created_at: "2026-09-28T00:00:00Z",
    payload: {
      type: "turn.terminal",
      state: "completed",
      answer_length: 0,
      reasoning_length: 0,
    },
  })}\n\n`;
}

test("sends once, accepts fragmented terminal event and same-origin credentials", async () => {
  const wire = terminal(7);
  const fetchMock = vi.fn().mockResolvedValue(streamResponse([wire.slice(0, 13), wire.slice(13)]));
  vi.stubGlobal("fetch", fetchMock);
  const events: string[] = [];
  await sendChatTurn(conversationId, body, new AbortController().signal, (event) =>
    events.push(event.payload.type),
  );
  expect(events).toEqual(["turn.terminal"]);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
    method: "POST",
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
});

test("rejects mismatched event identity without replaying POST", async () => {
  const fetchMock = vi.fn().mockResolvedValue(streamResponse([terminal(1, "wrong")]));
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    sendChatTurn(conversationId, body, new AbortController().signal, () => {}),
  ).rejects.toThrow("invalid chat event");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("does not retry a response that ends before terminal", async () => {
  const fetchMock = vi.fn().mockResolvedValue(streamResponse([": still loading\n\n"]));
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    sendChatTurn(conversationId, body, new AbortController().signal, () => {}),
  ).rejects.toThrow("chat stream interrupted");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("surfaces safe problem details and does not retry a refused POST", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ title: "Conflict", detail: "Revision changed" }), {
      status: 409,
      headers: { "content-type": "application/problem+json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    sendChatTurn(conversationId, body, new AbortController().signal, () => {}),
  ).rejects.toThrow("Revision changed");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("uses typed, same-origin history pages and encoded cursors", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ data: [], next_cursor: null })))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          conversation: { id: conversationId },
          messages: [],
          turns: [],
          attachments: [],
          event_cursor: 0,
        }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  await listChatConversations("time:id", 10);
  await getChatConversation(conversationId, 7, 20);
  expect(fetchMock.mock.calls[0]?.[0]).toContain("cursor=time%3Aid");
  expect(fetchMock.mock.calls[1]?.[0]).toContain("before_position=7");
  expect(fetchMock.mock.calls[1]?.[1]?.credentials).toBe("same-origin");
});
