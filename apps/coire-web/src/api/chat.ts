import type { components } from "./schema";
import { api, apiError } from "./client";
import { openEventStream, readEventStream } from "./eventStream";

export type ChatPickerResponse = components["schemas"]["ChatPickerResponse"];
export type ChatConversation = components["schemas"]["ChatConversation"];
export type ChatConversationPage = components["schemas"]["ChatConversationPage"];
export type ChatConversationDetail = components["schemas"]["ChatConversationDetail"];
export type ChatConversationCreate = components["schemas"]["ChatConversationCreate"];
export type ChatConversationUpdate = components["schemas"]["ChatConversationUpdate"];
export type ChatDeleteRequest = components["schemas"]["ChatDeleteRequest"];
export type ChatDeletionResult = components["schemas"]["ChatDeletionResult"];
export type ChatTurnCreate = components["schemas"]["ChatTurnCreate"];
export type ChatTurnDetail = components["schemas"]["ChatTurnDetail"];
export type ChatTurn = components["schemas"]["ChatTurn"];
export type ChatEvent = components["schemas"]["ChatEvent"];
export type ChatMessage = components["schemas"]["ChatMessage-Output"];
export type ChatPickerEntry = components["schemas"]["ChatPickerEntry"];

export function listChatModels(): Promise<ChatPickerResponse> {
  return api<ChatPickerResponse>("/api/v1/chat/models");
}

export function createChatConversation(body: ChatConversationCreate): Promise<ChatConversation> {
  return api<ChatConversation>("/api/v1/chat/conversations", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateChatConversation(
  conversationId: string,
  body: ChatConversationUpdate,
): Promise<ChatConversation> {
  return api<ChatConversation>(`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteChatConversation(
  conversationId: string,
  body: ChatDeleteRequest,
): Promise<ChatDeletionResult> {
  return api<ChatDeletionResult>(`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
    body: JSON.stringify(body),
  });
}

export function listChatConversations(
  cursor: string | null = null,
  limit = 50,
): Promise<ChatConversationPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  return api<ChatConversationPage>("/api/v1/chat/conversations?" + query.toString());
}

export function getChatConversation(
  conversationId: string,
  beforePosition?: number,
  limit = 50,
): Promise<ChatConversationDetail> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (beforePosition !== undefined) query.set("before_position", String(beforePosition));
  return api<ChatConversationDetail>(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}?${query.toString()}`,
  );
}

export function getChatTurn(conversationId: string, turnId: string): Promise<ChatTurnDetail> {
  return api<ChatTurnDetail>(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}`,
  );
}

export function stopChatTurn(
  conversationId: string,
  turnId: string,
  reason: components["schemas"]["ChatStopRequest"]["reason"] = "user_stop",
): Promise<ChatTurn> {
  return api<ChatTurn>(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}/stop`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );
}

export function openChatEvents(
  conversationId: string,
  cursor: number,
  signal: AbortSignal,
): Promise<Response> {
  return openEventStream(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/events`,
    `${conversationId}:${cursor}`,
    signal,
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
