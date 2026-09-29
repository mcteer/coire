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

test("code mode submits a repository research run with an explicit source", async () => {
  const workspaceId = "00000000-0000-0000-0000-000000000020";
  const codingConversation = { ...conversation, mode: "code" };
  const fetchMock = vi.fn().mockImplementation((url: string, options?: RequestInit) => {
    if (url === "/api/v1/chat/models") return Promise.resolve(json({ data: [model] }));
    if (url.startsWith("/api/v1/chat/models?")) return Promise.resolve(json({ data: [model] }));
    if (url === "/api/v1/chat/conversations" && options?.method === "GET")
      return Promise.resolve(json({ data: [], next_cursor: null }));
    if (url === "/api/v1/workspaces")
      return Promise.resolve(
        json([{ id: workspaceId, repository_url: "https://github.com/org/repo.git" }]),
      );
    if (url === "/api/v1/chat/conversations" && options?.method === "POST")
      return Promise.resolve(json(codingConversation, 201));
    if (url.endsWith("/turns") && options?.method === "POST")
      return Promise.resolve(
        stream(
          event(1, {
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
              action: "research",
              created_at: conversation.created_at,
              updated_at: conversation.updated_at,
            },
          }) +
            event(2, {
              type: "turn.terminal",
              state: "completed",
              answer_length: 0,
              reasoning_length: 0,
            }),
        ),
      );
    if (url.startsWith(`/api/v1/chat/conversations/${conversationId}?`))
      return Promise.resolve(
        json({
          conversation: { ...codingConversation, revision: 2 },
          messages: [],
          turns: [],
          attachments: [],
          event_cursor: 2,
        }),
      );
    if (url.endsWith("/events")) return Promise.resolve(stream(""));
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  fireEvent.click(screen.getByRole("button", { name: "Code" }));
  await screen.findByRole("combobox", { name: "Registered repository" });
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Registered repository" })).toHaveValue(
      workspaceId,
    ),
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Source revision" }), {
    target: { value: "main" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Task" }), {
    target: { value: "Find entry points" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) => String(url).endsWith("/turns") && options?.method === "POST",
      ),
    ).toBe(true),
  );
  const turnCall = fetchMock.mock.calls.find(
    ([url, options]) => String(url).endsWith("/turns") && options?.method === "POST",
  );
  const body = JSON.parse(String(turnCall?.[1]?.body));
  expect(body).toMatchObject({
    action: "research",
    workspace_id: workspaceId,
    source_revision: "main",
    content: "Find entry points",
  });
});

test("Apply requires a chosen plan and inherits its source revision", async () => {
  const workspaceId = "00000000-0000-0000-0000-000000000020";
  const callId = "00000000-0000-0000-0000-000000000021";
  const saved = { ...conversation, mode: "code", revision: 2, title: "Code work" };
  const planTurn = {
    id: "00000000-0000-0000-0000-000000000022",
    action: "plan",
    state: "completed",
    coding_call_id: callId,
    created_at: conversation.created_at,
  };
  const fetchMock = vi.fn().mockImplementation((url: string, options?: RequestInit) => {
    if (url.startsWith("/api/v1/chat/models")) return Promise.resolve(json({ data: [model] }));
    if (url === "/api/v1/workspaces")
      return Promise.resolve(
        json([{ id: workspaceId, repository_url: "https://github.com/org/repo.git" }]),
      );
    if (url.startsWith(`/api/v1/chat/conversations/${conversationId}/turns/${planTurn.id}`))
      return Promise.resolve(json({ turn: planTurn, coding_result: null }));
    if (url.endsWith("/turns") && options?.method === "POST")
      return Promise.resolve(
        stream(
          event(3, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 0,
            reasoning_length: 0,
          }),
        ),
      );
    if (url.startsWith(`/api/v1/chat/conversations/${conversationId}?`))
      return Promise.resolve(
        json({
          conversation: saved,
          turns: [planTurn],
          messages: [],
          attachments: [],
          event_cursor: 2,
        }),
      );
    if (url.startsWith("/api/v1/chat/conversations?") || url === "/api/v1/chat/conversations")
      return Promise.resolve(json({ data: [saved], next_cursor: null }));
    if (url.endsWith("/events")) return Promise.resolve(stream(""));
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  fireEvent.click(await screen.findByRole("button", { name: /Code work/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Apply" }));
  await screen.findByRole("combobox", { name: "Plan to apply" });
  fireEvent.change(screen.getByRole("textbox", { name: "Task" }), {
    target: { value: "Make the change" },
  });
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "Plan to apply" }), {
    target: { value: callId },
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) => String(url).endsWith("/turns") && options?.method === "POST",
      ),
    ).toBe(true),
  );
  const sent = fetchMock.mock.calls.find(
    ([url, options]) => String(url).endsWith("/turns") && options?.method === "POST",
  );
  const body = JSON.parse(String(sent?.[1]?.body));
  expect(body).toMatchObject({ action: "apply", plan_id: callId, workspace_id: workspaceId });
  expect(body).not.toHaveProperty("source_revision");
});

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

test("shows cold wait immediately while conversation creation is pending", async () => {
  let finishCreate: ((value: Response) => void) | undefined;
  const pendingCreate = new Promise<Response>((resolve) => {
    finishCreate = resolve;
  });
  const fetchMock = vi.fn().mockImplementation((url: string, options?: RequestInit) => {
    if (url === "/api/v1/chat/models")
      return Promise.resolve(
        json({ data: [{ ...model, source: "studio", load_state: "cold", estimated_warmup_seconds: 18.2 }] }),
      );
    if (url === "/api/v1/chat/conversations" && options?.method === "GET")
      return Promise.resolve(json({ data: [], next_cursor: null }));
    if (url === "/api/v1/chat/conversations" && options?.method === "POST") return pendingCreate;
    if (url.endsWith("/turns") && options?.method === "POST")
      return Promise.resolve(
        stream(
          event(1, {
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
              created_at: conversation.created_at,
              updated_at: conversation.updated_at,
            },
          }) +
            event(2, {
              type: "turn.terminal",
              state: "completed",
              answer_length: 0,
              reasoning_length: 0,
            }),
        ),
      );
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  await screen.findByRole("button", { name: /Friendly model/ });
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Hello" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(screen.getByRole("status")).toHaveTextContent("about 19 s");
  expect(fetchMock).toHaveBeenCalledWith("/api/v1/chat/conversations", expect.anything());
  await act(async () => finishCreate?.(json(conversation, 201)));
  await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeDisabled());
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

test("expired send prompts sign-in and keeps the unsent same-tab draft", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json({ data: [], next_cursor: null }))
    .mockResolvedValueOnce(json(conversation, 201))
    .mockResolvedValueOnce(json({ title: "Unauthorized" }, 401));
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  await screen.findByText("Friendly model");
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Keep this draft" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByRole("heading", { name: "Sign in again" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/");
  expect(loadChatDrafts(conversation.owner_id).get(conversationId)?.text).toBe("Keep this draft");
});

test("explicit retry keeps the partial answer and does not duplicate the user input", async () => {
  const oldTurnId = "00000000-0000-0000-0000-000000000077";
  const oldAnswerId = "00000000-0000-0000-0000-000000000078";
  const savedConversation = { ...conversation, title: "Interrupted", revision: 2 };
  const oldTurn = {
    id: oldTurnId,
    conversation_id: conversationId,
    client_request_id: crypto.randomUUID(),
    accepted_revision: 1,
    input_message_id: inputId,
    assistant_message_id: oldAnswerId,
    model_id: modelId,
    model_display_name: "Friendly model",
    state: "interrupted",
    action: "chat",
    created_at: conversation.created_at,
    updated_at: conversation.updated_at,
  };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json({ data: [savedConversation], next_cursor: null }))
    .mockResolvedValueOnce(
      json({
        conversation: savedConversation,
        messages: [
          {
            id: inputId,
            conversation_id: conversationId,
            position: 1,
            role: "user",
            text: "Prompt",
            reasoning: "",
            model_id: modelId,
            model_display_name: "Friendly model",
            attachment_ids: [],
            created_at: conversation.created_at,
          },
          {
            id: oldAnswerId,
            conversation_id: conversationId,
            position: 2,
            role: "assistant",
            text: "Saved partial",
            reasoning: "",
            model_id: modelId,
            model_display_name: "Friendly model",
            attachment_ids: [],
            created_at: conversation.created_at,
          },
        ],
        turns: [oldTurn],
        attachments: [],
        event_cursor: 2,
      }),
    )
    .mockResolvedValueOnce(
      stream(
        event(3, {
          type: "turn.accepted",
          turn: {
            ...oldTurn,
            id: turnId,
            input_message_id: inputId,
            assistant_message_id: answerId,
            client_request_id: crypto.randomUUID(),
            accepted_revision: 2,
            retry_of: oldTurnId,
            recovery_mode: "retry",
            state: "accepted",
          },
        }) +
          event(4, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 0,
            reasoning_length: 0,
          }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  const view = render(<Chat ownerId={conversation.owner_id} />);
  fireEvent.click(await screen.findByRole("button", { name: /Interrupted/ }));
  expect(await screen.findByRole("button", { name: "Retry response" })).toBeEnabled();
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "New unsent draft" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Retry response" }));
  await waitFor(() => expect(view.container.querySelectorAll("article.assistant")).toHaveLength(2));
  expect(view.container.querySelectorAll("article.user")).toHaveLength(1);
  expect(screen.getByText("Saved partial")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("New unsent draft");
  const posted = JSON.parse(fetchMock.mock.calls[3]?.[1]?.body as string);
  expect(posted.retry_of).toBe(oldTurnId);
  expect(posted.recovery_mode).toBe("retry");
  expect(posted.content).toBe("Prompt");
});

test("explicit continuation keeps the partial answer and creates a separate input", async () => {
  const oldTurnId = "00000000-0000-0000-0000-000000000087";
  const oldAnswerId = "00000000-0000-0000-0000-000000000088";
  const nextInputId = "00000000-0000-0000-0000-000000000089";
  const savedConversation = { ...conversation, title: "Continue me", revision: 2 };
  const oldTurn = {
    id: oldTurnId,
    conversation_id: conversationId,
    client_request_id: crypto.randomUUID(),
    accepted_revision: 1,
    input_message_id: inputId,
    assistant_message_id: oldAnswerId,
    model_id: modelId,
    model_display_name: "Friendly model",
    state: "interrupted",
    action: "chat",
    created_at: conversation.created_at,
    updated_at: conversation.updated_at,
  };
  const savedMessages = [
    {
      id: inputId,
      conversation_id: conversationId,
      position: 1,
      role: "user",
      text: "Prompt",
      reasoning: "",
      model_id: modelId,
      model_display_name: "Friendly model",
      attachment_ids: [],
      created_at: conversation.created_at,
    },
    {
      id: oldAnswerId,
      conversation_id: conversationId,
      position: 2,
      role: "assistant",
      text: "Saved partial",
      reasoning: "",
      model_id: modelId,
      model_display_name: "Friendly model",
      attachment_ids: [],
      created_at: conversation.created_at,
    },
  ];
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json({ data: [savedConversation], next_cursor: null }))
    .mockResolvedValueOnce(
      json({
        conversation: savedConversation,
        messages: savedMessages,
        turns: [oldTurn],
        attachments: [],
        event_cursor: 2,
      }),
    )
    .mockResolvedValueOnce(
      stream(
        event(3, {
          type: "turn.accepted",
          turn: {
            ...oldTurn,
            id: turnId,
            input_message_id: nextInputId,
            assistant_message_id: answerId,
            client_request_id: crypto.randomUUID(),
            accepted_revision: 2,
            retry_of: oldTurnId,
            recovery_mode: "continue",
            state: "accepted",
          },
        }) +
          event(4, {
            type: "turn.terminal",
            state: "completed",
            answer_length: 0,
            reasoning_length: 0,
          }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  const view = render(<Chat ownerId={conversation.owner_id} />);
  fireEvent.click(await screen.findByRole("button", { name: /Continue me/ }));
  const continueButton = await screen.findByRole("button", {
    name: "Continue from partial answer",
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Different draft" },
  });
  fireEvent.click(continueButton);
  await waitFor(() => expect(view.container.querySelectorAll("article.user")).toHaveLength(2));
  expect(view.container.querySelectorAll("article.assistant")).toHaveLength(2);
  expect(screen.getByText("Saved partial")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Different draft");
  const posted = JSON.parse(fetchMock.mock.calls[3]?.[1]?.body as string);
  expect(posted).toMatchObject({
    recovery_mode: "continue",
    retry_of: oldTurnId,
    content: "Continue the previous response.",
    attachments: [],
  });
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

test("restores saved file and PDF page choices only after owner-scoped detail confirms them", async () => {
  const fileId = "00000000-0000-0000-0000-000000000088";
  const savedConversation = { ...conversation, title: "Saved pages" };
  loadChatDrafts(conversation.owner_id);
  saveChatDrafts(
    conversation.owner_id,
    new Map([
      [
        conversationId,
        {
          text: "Read pages",
          modelId,
          files: [{ file_id: fileId, mode: "visual" as const, pages: [2, 4] }],
        },
      ],
    ]),
  );
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(json({ data: [model] }))
    .mockResolvedValueOnce(json({ data: [savedConversation], next_cursor: null }))
    .mockResolvedValueOnce(
      json({
        conversation: savedConversation,
        messages: [],
        event_cursor: 0,
        attachments: [
          {
            id: fileId,
            owner_id: conversation.owner_id,
            conversation_id: conversationId,
            filename: "pages.pdf",
            detected_type: "application/pdf",
            original_bytes: 100,
            original_sha256: "a".repeat(64),
            derived_bytes: 200,
            state: "ready",
            page_count: 4,
            created_at: conversation.created_at,
            updated_at: conversation.updated_at,
          },
        ],
      }),
    );
  vi.stubGlobal("fetch", fetchMock);
  render(<Chat ownerId={conversation.owner_id} />);
  fireEvent.click(await screen.findByRole("button", { name: /Saved pages/ }));
  expect(await screen.findByRole("checkbox", { name: "pages.pdf" })).toBeChecked();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Read pages");
  expect(screen.getByRole("combobox", { name: "Content mode for pages.pdf" })).toHaveValue(
    "visual",
  );
  expect(screen.getByLabelText("2")).toBeChecked();
  expect(screen.getByLabelText("4")).toBeChecked();
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  expect(screen.getByText(/Visual Chat is not available yet/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Remove visual selections" }));
  expect(screen.getByRole("checkbox", { name: "pages.pdf" })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
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
