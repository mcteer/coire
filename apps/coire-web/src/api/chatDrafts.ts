/** Same-tab text/model drafts only. The server remains the authority for ownership and eligibility. */
export type ChatDraft = { text: string; modelId: string | null };

const ownerKey = "coire.chat.draft-owner";
const prefix = "coire.chat.drafts.";
const idPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const maxEntries = 20;
const maxText = 64 * 1024;

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
      const draft = value as { text?: unknown; modelId?: unknown };
      if (typeof draft.text !== "string" || new TextEncoder().encode(draft.text).length > maxText)
        continue;
      if (
        draft.modelId !== null &&
        draft.modelId !== undefined &&
        (typeof draft.modelId !== "string" || !idPattern.test(draft.modelId))
      )
        continue;
      result.set(key, {
        text: draft.text,
        modelId: typeof draft.modelId === "string" ? draft.modelId : null,
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
    const entries = [...drafts]
      .filter(
        ([key, value]) =>
          (key === "new" || idPattern.test(key)) &&
          new TextEncoder().encode(value.text).length <= maxText &&
          (value.modelId === null || idPattern.test(value.modelId)),
      )
      .slice(-maxEntries);
    sessionStorage.setItem(prefix + ownerId, JSON.stringify(entries));
  } catch {
    // In-memory drafts still work when storage is unavailable.
  }
}
