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
export type ChatAttachment = components["schemas"]["ChatAttachment"];
export type ChatAttachmentSelection = components["schemas"]["ChatAttachmentSelection"];
export type ChatFileProcessRequest = components["schemas"]["ChatFileProcessRequest"];
export type RegisteredWorkspace = components["schemas"]["RegisteredWorkspace"];
export type ChatRunActivity = components["schemas"]["ChatRunActivity"];
export type ChatRunActivityStatus = components["schemas"]["ChatRunActivityStatus"];
export type ChatTurnResult = components["schemas"]["ChatTurnResult"];
export type BranchArtifact = components["schemas"]["BranchArtifact"];

function filePath(conversationId: string, fileId: string): string {
  return `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/files/${encodeURIComponent(fileId)}`;
}

export async function uploadChatFile(
  conversationId: string,
  file: File,
  expectedRevision: number,
): Promise<ChatAttachment> {
  const form = new FormData();
  form.set("filename", file.name);
  form.set("expected_revision", String(expectedRevision));
  form.set("file", file);
  const response = await fetch(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/files`,
    { method: "POST", credentials: "same-origin", body: form },
  );
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as ChatAttachment;
}

export function getChatFile(conversationId: string, fileId: string): Promise<ChatAttachment> {
  return api<ChatAttachment>(filePath(conversationId, fileId));
}

export function processChatFile(
  conversationId: string,
  fileId: string,
  body: ChatFileProcessRequest,
): Promise<ChatAttachment> {
  return api<ChatAttachment>(`${filePath(conversationId, fileId)}/process`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function chatFileDownloadUrl(conversationId: string, fileId: string): string {
  return `${filePath(conversationId, fileId)}/content`;
}

export function chatFilePreviewUrl(
  conversationId: string,
  fileId: string,
  assetId: string,
): string {
  return `${filePath(conversationId, fileId)}/previews/${encodeURIComponent(assetId)}`;
}

export function listChatModels(
  mode: "chat" | "code" = "chat",
  action: "chat" | "research" | "plan" | "apply" = "chat",
): Promise<ChatPickerResponse> {
  if (mode === "chat") return api<ChatPickerResponse>("/api/v1/chat/models");
  return api<ChatPickerResponse>(`/api/v1/chat/models?${new URLSearchParams({ mode, action })}`);
}

export function listRegisteredWorkspaces(): Promise<RegisteredWorkspace[]> {
  return api<RegisteredWorkspace[]>("/api/v1/workspaces");
}

export function registerWorkspace(repositoryUrl: string): Promise<RegisteredWorkspace> {
  return api<RegisteredWorkspace>("/api/v1/workspaces", {
    method: "POST",
    body: JSON.stringify({ repository_url: repositoryUrl }),
  });
}

export function chatArtifactUrl(conversationId: string, turnId: string): string {
  return `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}/artifact`;
}

export function getBranchArtifactMetadata(artifactId: string): Promise<BranchArtifact> {
  return api<BranchArtifact>(`/api/v1/mcp/artifacts/${encodeURIComponent(artifactId)}/metadata`);
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
  return api<ChatDeletionResult>(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}`,
    {
      method: "DELETE",
      body: JSON.stringify(body),
    },
  );
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
      (cursor !== null && event.cursor <= cursor) ||
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
