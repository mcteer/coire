import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Chat } from "./Chat";
import { loadChatDrafts, saveChatDrafts } from "../api/chatDrafts";

afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});

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
    .mockResolvedValueOnce(json({ data: [], next_cursor: null }))
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
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Hi" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText("Hello!")).toBeInTheDocument());
  expect(screen.getByText("Hi")).toBeInTheDocument();
  expect(screen.getAllByText("Friendly model").length).toBeGreaterThan(1);
  expect(fetchMock).toHaveBeenCalledTimes(4);
  expect(loadChatDrafts(conversation.owner_id).get(conversationId)).toBeUndefined();
});

test("creates a conversation before uploading and sends an explicit ready text selection", async () => {
  const fileId = "00000000-0000-0000-0000-000000000019";
  const attachment = {
    id: fileId,
    owner_id: conversation.owner_id,
    conversation_id: conversationId,
    filename: "notes.txt",
    detected_type: "text/plain",
    original_bytes: 5,
    original_sha256: "a".repeat(64),
    derived_bytes: 0,
    state: "ready",
    created_at: "2026-09-28T00:00:00Z",
    updated_at: "2026-09-28T00:00:00Z",
  };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json({ data: [], next_cursor: null }))
    .mockResolvedValueOnce(json(conversation, 201))
    .mockResolvedValueOnce(json({ ...attachment, state: "processing" }, 202))
    .mockResolvedValueOnce(
      json({
        conversation: { ...conversation, revision: 2 },
        messages: [],
        attachments: [attachment],
        event_cursor: 1,
      }),
    )
    .mockResolvedValueOnce(
      stream(
        event(2, {
          type: "turn.accepted",
          turn: {
            id: turnId,
            conversation_id: conversationId,
            client_request_id: crypto.randomUUID(),
            accepted_revision: 2,
            input_message_id: inputId,
            assistant_message_id: answerId,
            model_id: modelId,
            model_display_name: "Friendly model",
            state: "accepted",
            action: "chat",
            created_at: "2026-09-28T00:00:00Z",
            updated_at: "2026-09-28T00:00:00Z",
          },
        }) +
          event(3, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 0,
            reasoning_length: 0,
          }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByLabelText("Add file"), {
    target: { files: [new File(["hello"], "notes.txt", { type: "text/plain" })] },
  });
  await waitFor(() => expect(screen.getByText("notes.txt")).toBeInTheDocument());
  await waitFor(() => expect(screen.getByLabelText("notes.txt")).toBeEnabled());
  expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/v1/chat/conversations");
  expect(fetchMock.mock.calls[3]?.[0]).toBe(`/api/v1/chat/conversations/${conversationId}/files`);
  const form = fetchMock.mock.calls[3]?.[1]?.body as FormData;
  expect(form.get("expected_revision")).toBe("1");
  fireEvent.click(screen.getByLabelText("notes.txt"));
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Read it" },
  });
  expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(6));
  const sent = JSON.parse(fetchMock.mock.calls[5]?.[1]?.body as string);
  expect(sent.attachments).toEqual([{ file_id: fileId, mode: "text" }]);
  expect(await screen.findByText("notes.txt · Extracted text")).toBeInTheDocument();
});

test("Stop requests server cancellation and keeps the saved partial answer", async () => {
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
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(value) {
        controller = value;
      },
    }),
    { headers: { "content-type": "text/event-stream" } },
  );
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json({ data: [], next_cursor: null }))
    .mockResolvedValueOnce(json(conversation, 201))
    .mockResolvedValueOnce(response)
    .mockResolvedValueOnce(json({ ...accepted.turn, state: "stop_requested" }));
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Keep this" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
  await act(async () => {
    controller.enqueue(
      new TextEncoder().encode(
        event(1, accepted) +
          event(2, {
            type: "message.delta",
            message_id: answerId,
            channel: "answer",
            text: "Partial",
            offset: 7,
          }),
      ),
    );
  });
  expect(screen.getByText("Partial")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Stop response" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));
  expect(fetchMock.mock.calls[4]?.[0]).toContain(`/turns/${turnId}/stop`);
  expect(JSON.parse(fetchMock.mock.calls[4]?.[1]?.body as string)).toEqual({ reason: "user_stop" });
  expect(screen.getByRole("status")).toHaveTextContent("Stopping response");
  await act(async () => {
    controller.enqueue(
      new TextEncoder().encode(
        event(3, {
          type: "turn.terminal",
          state: "stopped",
          answer_length: 7,
          reasoning_length: 0,
          safe_error: "stopped by user",
        }),
      ),
    );
    controller.close();
  });
  await waitFor(() => expect(screen.queryByRole("button", { name: "Stop response" })).toBeNull());
  expect(screen.getByText("Partial")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("stopped by user");
});

test("leaving an owned stream requests navigation Stop before switching conversations", async () => {
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
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (url === "/api/v1/chat/models") return Promise.resolve(json({ data: [model] }));
    if (url.startsWith("/api/v1/chat/conversations?"))
      return Promise.resolve(json({ data: [], next_cursor: null }));
    if (url === "/api/v1/chat/conversations") return Promise.resolve(json(conversation, 201));
    if (url.endsWith(`/turns/${turnId}/stop`))
      return Promise.resolve(json({ ...accepted.turn, state: "stop_requested" }));
    if (url.endsWith("/turns")) {
      init?.signal?.addEventListener("abort", () => {
        controller.error(new DOMException("aborted", "AbortError"));
      });
      return Promise.resolve(
        new Response(
          new ReadableStream<Uint8Array>({
            start(value) {
              controller = value;
            },
          }),
          { headers: { "content-type": "text/event-stream" } },
        ),
      );
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Navigate away" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
  await act(async () => {
    controller.enqueue(new TextEncoder().encode(event(1, accepted)));
  });
  expect(screen.getByRole("button", { name: "New conversation" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));
  expect(JSON.parse(fetchMock.mock.calls[4]?.[1]?.body as string)).toEqual({
    reason: "navigation",
  });
  await waitFor(() => expect(screen.queryByText("Navigate away")).toBeNull());
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a selected viewing tab reconciles another tab's saved turn without a POST", async () => {
  const saved = { ...conversation, title: "Shared view" };
  let details = 0;
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url === "/api/v1/chat/models") return Promise.resolve(json({ data: [model] }));
    if (url.startsWith("/api/v1/chat/conversations?"))
      return Promise.resolve(json({ data: [saved], next_cursor: null }));
    if (url.endsWith("/events"))
      return Promise.resolve(
        stream(
          event(1, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 16,
            reasoning_length: 0,
          }),
        ),
      );
    if (url.startsWith(`/api/v1/chat/conversations/${conversationId}?`)) {
      details += 1;
      return Promise.resolve(
        json({
          conversation: saved,
          messages:
            details === 1
              ? []
              : [
                  {
                    id: answerId,
                    conversation_id: conversationId,
                    position: 2,
                    role: "assistant",
                    text: "Other tab answer",
                    reasoning: "",
                    model_id: modelId,
                    model_display_name: "Friendly model",
                    attachment_ids: [],
                    created_at: "2026-09-28T00:00:00Z",
                  },
                ],
          turns: [],
          attachments: [],
          event_cursor: details === 1 ? 0 : 1,
        }),
      );
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  fireEvent.click(await screen.findByRole("button", { name: /Shared view/ }));
  expect(await screen.findByText("Other tab answer", {}, { timeout: 2000 })).toBeInTheDocument();
  expect(
    fetchMock.mock.calls.every(
      ([url, options]) => !String(url).endsWith("/turns") || options?.method !== "POST",
    ),
  ).toBe(true);
});

test("preserves the draft when admission refuses the send", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json({ data: [], next_cursor: null }))
    .mockResolvedValueOnce(json(conversation, 201))
    .mockResolvedValueOnce(json({ title: "Conflict", detail: "Conversation changed" }, 409));
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Keep me" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Conversation changed"));
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Keep me");
  expect(loadChatDrafts(conversation.owner_id).get(conversationId)?.text).toBe("Keep me");
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
    .mockResolvedValueOnce(json({ data: [], next_cursor: null }))
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
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Retry me" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("network lost"));
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText("Retry me")).toBeInTheDocument());
  const first = JSON.parse(fetchMock.mock.calls[3]?.[1]?.body as string) as {
    client_request_id: string;
  };
  const second = JSON.parse(fetchMock.mock.calls[4]?.[1]?.body as string) as {
    client_request_id: string;
  };
  expect(first.client_request_id).toBe(second.client_request_id);
  expect(fetchMock).toHaveBeenCalledTimes(5);
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
    .mockResolvedValueOnce(json({ data: [], next_cursor: null }))
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
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
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
  expect(fetchMock).toHaveBeenCalledTimes(5);
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
      .mockResolvedValueOnce(json({ data: [], next_cursor: null }))
      .mockResolvedValueOnce(json(conversation, 201))
      .mockResolvedValueOnce(response),
  );
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
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

test("reopens saved partial output with model attribution and older messages", async () => {
  const saved = { ...conversation, title: "Saved notes", active_turn_id: turnId };
  const savedAnswer = {
    id: answerId,
    conversation_id: conversationId,
    position: 4,
    role: "assistant",
    text: "Saved partial",
    reasoning: "",
    model_id: modelId,
    model_display_name: "Earlier model",
    attachment_ids: [],
    created_at: "2026-09-28T00:00:00Z",
  };
  const older = { ...savedAnswer, id: inputId, position: 2, text: "Older answer" };
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url === "/api/v1/chat/models") return Promise.resolve(json({ data: [model] }));
    if (url.startsWith("/api/v1/chat/conversations?")) {
      return Promise.resolve(json({ data: [saved], next_cursor: null }));
    }
    if (url.includes("before_position=4")) {
      return Promise.resolve(
        json({
          conversation: saved,
          messages: [older],
          turns: [],
          attachments: [],
          event_cursor: 6,
          next_message_position: null,
        }),
      );
    }
    return Promise.resolve(
      json({
        conversation: saved,
        messages: [savedAnswer],
        turns: [],
        attachments: [],
        event_cursor: 6,
        next_message_position: 4,
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
  fireEvent.click(await screen.findByRole("button", { name: /Saved notes/ }));
  expect(await screen.findByText("Saved partial")).toBeInTheDocument();
  expect(screen.getByText("Saved partial").closest("article")).toHaveTextContent("Earlier model");
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Load older messages" }));
  expect(await screen.findByText("Older answer")).toBeInTheDocument();
});

test("late history response cannot replace a newer selected conversation", async () => {
  const first = { ...conversation, title: "First saved" };
  const second = {
    ...conversation,
    id: "00000000-0000-0000-0000-000000000099",
    title: "Second saved",
  };
  let resolveFirst!: (response: Response) => void;
  const firstResponse = new Promise<Response>((resolve) => {
    resolveFirst = resolve;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/chat/models") return Promise.resolve(json({ data: [model] }));
      if (url.startsWith("/api/v1/chat/conversations?")) {
        return Promise.resolve(json({ data: [first, second], next_cursor: null }));
      }
      if (url.includes(first.id)) return firstResponse;
      return Promise.resolve(
        json({
          conversation: second,
          messages: [
            {
              id: answerId,
              conversation_id: second.id,
              position: 1,
              role: "assistant",
              text: "Second content",
              reasoning: "",
              model_id: modelId,
              model_display_name: "Friendly model",
              attachment_ids: [],
              created_at: "2026-09-28T00:00:00Z",
            },
          ],
          turns: [],
          attachments: [],
          event_cursor: 2,
        }),
      );
    }),
  );
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
  fireEvent.click(await screen.findByRole("button", { name: /First saved/ }));
  fireEvent.click(screen.getByRole("button", { name: /Second saved/ }));
  expect(await screen.findByText("Second content")).toBeInTheDocument();
  await act(async () => {
    resolveFirst(
      json({ conversation: first, messages: [], turns: [], attachments: [], event_cursor: 1 }),
    );
  });
  expect(screen.getByText("Second content")).toBeInTheDocument();
});

test("keeps separate unsent drafts while navigating saved and new conversations", async () => {
  const saved = { ...conversation, title: "Draft notes" };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/chat/models") return Promise.resolve(json({ data: [model] }));
      if (url.startsWith("/api/v1/chat/conversations?")) {
        return Promise.resolve(json({ data: [saved], next_cursor: null }));
      }
      return Promise.resolve(
        json({
          conversation: saved,
          messages: [],
          turns: [],
          attachments: [],
          event_cursor: 0,
        }),
      );
    }),
  );
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "New draft" },
  });
  fireEvent.click(await screen.findByRole("button", { name: /Draft notes/ }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(""));
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Saved draft" },
  });
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  await waitFor(() =>
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("New draft"),
  );
  fireEvent.click(screen.getByRole("button", { name: /Draft notes/ }));
  await waitFor(() =>
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Saved draft"),
  );
});

test("shows a clear unavailable state while the server release flag is off", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => Promise.resolve(json({ title: "Not Found" }, 404))),
  );
  render(<Chat ownerId="00000000-0000-0000-0000-000000000006" />);
  expect(await screen.findByRole("heading", { name: "Chat is unavailable" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
});

test("restores same-tab text and an eligible model after reload", async () => {
  const second = {
    ...model,
    id: "00000000-0000-0000-0000-000000000099",
    display_name: "Saved choice",
  };
  loadChatDrafts(conversation.owner_id);
  saveChatDrafts(
    conversation.owner_id,
    new Map([["new", { text: "Recovered draft", modelId: second.id }]]),
  );
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation((url: string) =>
        Promise.resolve(
          url === "/api/v1/chat/models"
            ? json({ data: [model, second] })
            : json({ data: [], next_cursor: null }),
        ),
      ),
  );
  render(<Chat ownerId={conversation.owner_id} />);
  expect(await screen.findByRole("button", { name: /Saved choice/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Recovered draft");
});

test("changing verified identity in one tab clears the former owner's draft", async () => {
  loadChatDrafts(conversation.owner_id);
  saveChatDrafts(
    conversation.owner_id,
    new Map([["new", { text: "Owner A private draft", modelId: null }]]),
  );
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation((url: string) =>
        Promise.resolve(
          url === "/api/v1/chat/models"
            ? json({ data: [model] })
            : json({ data: [], next_cursor: null }),
        ),
      ),
  );
  const view = render(<Chat ownerId={conversation.owner_id} />);
  await screen.findByRole("button", { name: /Friendly model/ });
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Owner A private draft");
  const other = "00000000-0000-0000-0000-000000000010";
  await act(async () => view.rerender(<Chat ownerId={other} />));
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
  expect(sessionStorage.getItem("coire.chat.drafts." + conversation.owner_id)).toBeNull();
});
