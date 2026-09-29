import type { components } from "./schema";
import { api, apiError } from "./client";
import { readEventStream } from "./eventStream";

export type ChatPickerResponse = components["schemas"]["ChatPickerResponse"];
export type ChatConversation = components["schemas"]["ChatConversation"];
export type ChatConversationCreate = components["schemas"]["ChatConversationCreate"];
export type ChatTurnCreate = components["schemas"]["ChatTurnCreate"];
export type ChatTurnDetail = components["schemas"]["ChatTurnDetail"];
export type ChatEvent = components["schemas"]["ChatEvent"];

export function listChatModels(): Promise<ChatPickerResponse> {
  return api<ChatPickerResponse>("/api/v1/chat/models");
}

export function createChatConversation(body: ChatConversationCreate): Promise<ChatConversation> {
  return api<ChatConversation>("/api/v1/chat/conversations", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getChatTurn(conversationId: string, turnId: string): Promise<ChatTurnDetail> {
  return api<ChatTurnDetail>(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}`,
  );
}

/** One user action, one POST. The caller may observe status after interruption. */
export async function sendChatTurn(
  conversationId: string,
  body: ChatTurnCreate,
  signal: AbortSignal,
  onEvent: (event: ChatEvent) => void,
): Promise<void> {
  const response = await fetch(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/turns`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  if (!response.ok) throw await apiError(response);
  if (!response.headers.get("content-type")?.startsWith("text/event-stream")) {
    throw new Error("chat stream unavailable");
  }
  let cursor: number | null = null;
  let terminal = false;
  await readEventStream(response, signal, (frame) => {
    let value: unknown;
    try {
      value = JSON.parse(frame.data);
    } catch {
      throw new Error("invalid chat event");
    }
    if (!value || typeof value !== "object") throw new Error("invalid chat event");
    const event = value as ChatEvent;
    if (
      event.conversation_id !== conversationId ||
      !Number.isSafeInteger(event.cursor) ||
      event.cursor < 1 ||
      !event.payload ||
      typeof event.payload.type !== "string" ||
      frame.id !== `${conversationId}:${event.cursor}` ||
      frame.event !== event.payload.type ||
      (cursor !== null && event.cursor !== cursor + 1) ||
      terminal
    ) {
      throw new Error("invalid chat event");
    }
    cursor = event.cursor;
    terminal = event.payload.type === "turn.terminal";
    onEvent(event);
  });
  if (!signal.aborted && !terminal) throw new Error("chat stream interrupted");
}
