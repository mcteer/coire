/** Authenticated transport for admin SSE; callers own parsing and lifecycle. */
export function openEventStream(url: string, cursor: string | null, signal: AbortSignal) {
  return fetch(url, {
    credentials: "same-origin",
    headers: cursor ? { "Last-Event-ID": cursor } : {},
    signal,
  });
}
