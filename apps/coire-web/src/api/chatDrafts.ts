import type { ChatAttachmentSelection } from "./chat";

/** Same-tab identifiers and choices only. The server remains the owner/eligibility authority. */
export type ChatDraft = {
  text: string;
  modelId: string | null;
  files?: ChatAttachmentSelection[];
};

const ownerKey = "coire.chat.draft-owner";
const prefix = "coire.chat.drafts.";
const idPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const maxEntries = 20;
const maxText = 64 * 1024;

function selectedFiles(value: unknown): ChatAttachmentSelection[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 10) return null;
  const files: ChatAttachmentSelection[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") return null;
    const selection = item as Record<string, unknown>;
    if (
      typeof selection.file_id !== "string" ||
      !idPattern.test(selection.file_id) ||
      (selection.mode !== "text" && selection.mode !== "visual") ||
      files.some((known) => known.file_id === selection.file_id)
    )
      return null;
    const pages = selection.pages === undefined ? [] : selection.pages;
    if (
      !Array.isArray(pages) ||
      pages.length > 10 ||
      pages.some((page) => !Number.isInteger(page) || page < 1 || page > 50) ||
      new Set(pages).size !== pages.length ||
      (selection.mode === "text" && pages.length > 0)
    )
      return null;
    files.push({ file_id: selection.file_id, mode: selection.mode, pages });
  }
  return files;
}

export function loadChatDrafts(ownerId: string): Map<string, ChatDraft> {
  const result = new Map<string, ChatDraft>();
  if (!idPattern.test(ownerId)) return result;
  try {
    const previous = sessionStorage.getItem(ownerKey);
    if (previous && previous !== ownerId) sessionStorage.removeItem(prefix + previous);
    sessionStorage.setItem(ownerKey, ownerId);
    const raw = sessionStorage.getItem(prefix + ownerId);
    if (!raw || raw.length > 2 * 1024 * 1024) return result;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return result;
    for (const item of parsed.slice(-maxEntries)) {
      if (!Array.isArray(item) || item.length !== 2) continue;
      const [key, value] = item as [unknown, unknown];
      if (typeof key !== "string" || (key !== "new" && !idPattern.test(key))) continue;
      if (!value || typeof value !== "object") continue;
      const draft = value as { text?: unknown; modelId?: unknown; files?: unknown };
      if (typeof draft.text !== "string" || new TextEncoder().encode(draft.text).length > maxText)
        continue;
      if (
        draft.modelId !== null &&
        draft.modelId !== undefined &&
        (typeof draft.modelId !== "string" || !idPattern.test(draft.modelId))
      )
        continue;
      const files = selectedFiles(draft.files);
      if (files === null || (key === "new" && files.length > 0)) continue;
      result.set(key, {
        text: draft.text,
        modelId: typeof draft.modelId === "string" ? draft.modelId : null,
        ...(files.length ? { files } : {}),
      });
    }
  } catch {
    // Storage may be unavailable in a private or restricted browsing context.
  }
  return result;
}

export function saveChatDrafts(ownerId: string, drafts: Map<string, ChatDraft>): void {
  if (!idPattern.test(ownerId)) return;
  try {
    const entries: [string, ChatDraft][] = [];
    for (const [key, value] of drafts) {
      const files = selectedFiles(value.files);
      if (
        (key !== "new" && !idPattern.test(key)) ||
        typeof value.text !== "string" ||
        new TextEncoder().encode(value.text).length > maxText ||
        (value.modelId !== null && !idPattern.test(value.modelId)) ||
        files === null ||
        (key === "new" && files.length > 0)
      )
        continue;
      entries.push([
        key,
        { text: value.text, modelId: value.modelId, ...(files.length ? { files } : {}) },
      ]);
    }
    sessionStorage.setItem(prefix + ownerId, JSON.stringify(entries.slice(-maxEntries)));
  } catch {
    // In-memory drafts still work when storage is unavailable.
  }
}
