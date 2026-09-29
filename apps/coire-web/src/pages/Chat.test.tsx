import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Chat } from "./Chat";

afterEach(() => vi.restoreAllMocks());

const conversationId = "00000000-0000-0000-0000-000000000001";
const modelId = "00000000-0000-0000-0000-000000000002";
const inputId = "00000000-0000-0000-0000-000000000003";
const answerId = "00000000-0000-0000-0000-000000000004";
const turnId = "00000000-0000-0000-0000-000000000005";
const model = {
  id: modelId,
  display_name: "Friendly model",
  tags: ["general"],
  context_window: 4096,
  size_class: "small",
  load_state: "loaded",
  verified: true,
  accepts_images: false,
};
const conversation = {
  id: conversationId,
  owner_id: "00000000-0000-0000-0000-000000000006",
  title: "New conversation",
  mode: "chat",
  selected_model_id: modelId,
  revision: 1,
  active_turn_id: null,
  created_at: "2026-09-28T00:00:00Z",
  updated_at: "2026-09-28T00:00:00Z",
};
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
function event(cursor: number, payload: object): string {
  const type = (payload as { type: string }).type;
  return `event: ${type}\nid: ${conversationId}:${cursor}\ndata: ${JSON.stringify({
    conversation_id: conversationId,
    cursor,
    turn_id: turnId,
    created_at: "2026-09-28T00:00:00Z",
    payload,
  })}\n\n`;
}
function stream(value: string): Response {
  return new Response(value, { headers: { "content-type": "text/event-stream" } });
}

test("shows accepted input, streamed answer and model snapshot", async () => {
  const accepted = {
    type: "turn.accepted",
    turn: {
      id: turnId,
      conversation_id: conversationId,
      client_request_id: crypto.randomUUID(),
      accepted_revision: 1,
      input_message_id: inputId,
      assistant_message_id: answerId,
      model_id: modelId,
      model_display_name: "Friendly model",
      state: "accepted",
      action: "chat",
      created_at: "2026-09-28T00:00:00Z",
      updated_at: "2026-09-28T00:00:00Z",
    },
  };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json(conversation, 201))
    .mockResolvedValueOnce(
      stream(
        event(1, accepted) +
          event(2, {
            type: "message.delta",
            message_id: answerId,
            channel: "answer",
            text: "Hello!",
            offset: 6,
          }) +
          event(3, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 6,
            reasoning_length: 0,
          }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Hi" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText("Hello!")).toBeInTheDocument());
  expect(screen.getByText("Hi")).toBeInTheDocument();
  expect(screen.getAllByText("Friendly model").length).toBeGreaterThan(1);
  expect(fetchMock).toHaveBeenCalledTimes(3);
});

test("preserves the draft when admission refuses the send", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json(conversation, 201))
    .mockResolvedValueOnce(json({ title: "Conflict", detail: "Conversation changed" }, 409));
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Keep me" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Conversation changed"));
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Keep me");
});

test("reuses request identity after an uncertain network failure", async () => {
  const accepted = {
    type: "turn.accepted",
    turn: {
      id: turnId,
      conversation_id: conversationId,
      client_request_id: "saved",
      accepted_revision: 1,
      input_message_id: inputId,
      assistant_message_id: answerId,
      model_id: modelId,
      model_display_name: "Friendly model",
      state: "accepted",
      action: "chat",
      created_at: "2026-09-28T00:00:00Z",
      updated_at: "2026-09-28T00:00:00Z",
    },
  };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json(conversation, 201))
    .mockRejectedValueOnce(new TypeError("network lost"))
    .mockResolvedValueOnce(
      stream(
        event(1, accepted) +
          event(2, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 0,
            reasoning_length: 0,
          }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Retry me" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("network lost"));
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText("Retry me")).toBeInTheDocument());
  const first = JSON.parse(fetchMock.mock.calls[2]?.[1]?.body as string) as {
    client_request_id: string;
  };
  const second = JSON.parse(fetchMock.mock.calls[3]?.[1]?.body as string) as {
    client_request_id: string;
  };
  expect(first.client_request_id).toBe(second.client_request_id);
  expect(fetchMock).toHaveBeenCalledTimes(4);
});

test("switches models within one conversation and keeps each answer attribution", async () => {
  const secondId = "00000000-0000-0000-0000-000000000007";
  const second = { ...model, id: secondId, display_name: "Second model" };
  const accepted = (revision: number, id: string, name: string, user: string, answer: string) => ({
    type: "turn.accepted",
    turn: {
      id: turnId,
      conversation_id: conversationId,
      client_request_id: "request",
      accepted_revision: revision,
      input_message_id: user,
      assistant_message_id: answer,
      model_id: id,
      model_display_name: name,
      state: "accepted",
      action: "chat",
      created_at: "2026-09-28T00:00:00Z",
      updated_at: "2026-09-28T00:00:00Z",
    },
  });
  const secondInput = "00000000-0000-0000-0000-000000000008";
  const secondAnswer = "00000000-0000-0000-0000-000000000009";
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model, second] }))
    .mockResolvedValueOnce(json(conversation, 201))
    .mockResolvedValueOnce(
      stream(
        event(1, accepted(1, modelId, "Friendly model", inputId, answerId)) +
          event(2, {
            type: "message.delta",
            message_id: answerId,
            channel: "answer",
            text: "First answer",
            offset: 12,
          }) +
          event(3, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 12,
            reasoning_length: 0,
          }),
      ),
    )
    .mockResolvedValueOnce(
      stream(
        event(4, accepted(2, secondId, "Second model", secondInput, secondAnswer)) +
          event(5, {
            type: "message.delta",
            message_id: secondAnswer,
            channel: "answer",
            text: "Second answer",
            offset: 13,
          }) +
          event(6, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 13,
            reasoning_length: 0,
          }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat />);
  await screen.findByRole("button", { name: /Friendly model/ });
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "First" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("First answer");
  fireEvent.click(screen.getByRole("button", { name: /Second model/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Second" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("Second answer");
  expect(screen.getByText("First answer").closest("article")).toHaveTextContent("Friendly model");
  expect(screen.getByText("Second answer").closest("article")).toHaveTextContent("Second model");
  expect(fetchMock).toHaveBeenCalledTimes(4);
});

test.each([
  [null, /estimate unavailable/],
  [41.2, /about 42 s/],
])("shows measured or unknown warm-up during the stream (%s)", async (estimate, label) => {
  const accepted = {
    type: "turn.accepted",
    turn: {
      id: turnId,
      conversation_id: conversationId,
      client_request_id: "request",
      accepted_revision: 1,
      input_message_id: inputId,
      assistant_message_id: answerId,
      model_id: modelId,
      model_display_name: "Friendly model",
      state: "accepted",
      action: "chat",
      created_at: "2026-09-28T00:00:00Z",
      updated_at: "2026-09-28T00:00:00Z",
    },
  };
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(value) {
        controller = value;
        value.enqueue(
          new TextEncoder().encode(
            event(1, accepted) +
              event(2, { type: "turn.status", state: "loading", estimate_seconds: estimate }),
          ),
        );
      },
    }),
    { headers: { "content-type": "text/event-stream" } },
  );
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(
        json({ data: [{ ...model, load_state: "cold", estimated_warmup_seconds: estimate }] }),
      )
      .mockResolvedValueOnce(json(conversation, 201))
      .mockResolvedValueOnce(response),
  );
  render(<Chat />);
  await screen.findByRole("button", { name: /Friendly model/ });
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Hi" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(label));
  await act(async () => {
    controller.enqueue(
      new TextEncoder().encode(
        event(3, {
          type: "turn.terminal",
          state: "failed",
          answer_length: 0,
          reasoning_length: 0,
          safe_error: "model warm-up failed; try again or choose another model",
        }),
      ),
    );
    controller.close();
  });
  expect(screen.getByRole("status")).toHaveTextContent(/choose another model/);
});
