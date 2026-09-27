import { useEffect, useRef, useState } from "react";
import { openEventStream } from "../api/eventStream";

type State<T> = { data: T | null; connected: boolean; error: string | null };

export function useEventStream<T>(url: string, initial: T | null = null): State<T> {
  const [state, setState] = useState<State<T>>({ data: initial, connected: false, error: null });
  const lastId = useRef<string | null>(null);

  useEffect(() => {
    if (url === "") return;
    let disposed = false;
    let controller: AbortController | null = null;
    let retry: number | undefined;
    let failures = 0;

    const clearRetry = () => {
      if (retry !== undefined) window.clearTimeout(retry);
      retry = undefined;
    };
    const canConnect = () => document.visibilityState !== "hidden" && navigator.onLine !== false;
    const connect = async () => {
      if (disposed || !canConnect() || controller) return;
      const active = new AbortController();
      controller = active;
      try {
        const response = await openEventStream(url, lastId.current, active.signal);
        if (!response.ok || !response.body) throw new Error(`stream refused (${response.status})`);
        setState((value) => ({ ...value, connected: true, error: null }));
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        try {
          while (!active.signal.aborted) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const blocks = buffer.split("\n\n");
            buffer = blocks.pop() ?? "";
            for (const block of blocks) {
              const id = block.match(/^id: (.+)$/m)?.[1];
              const payload = block.match(/^data: (.+)$/m)?.[1];
              if (!payload) continue;
              const event = JSON.parse(payload) as { snapshot: T };
              if (id) lastId.current = id;
              failures = 0;
              setState({ data: event.snapshot, connected: true, error: null });
            }
          }
        } finally {
          await reader.cancel().catch(() => undefined);
        }
        if (!active.signal.aborted) throw new Error("stream ended");
      } catch (error) {
        if (!active.signal.aborted && !disposed) {
          setState((value) => ({ ...value, connected: false, error: String(error) }));
        }
      } finally {
        if (controller === active) controller = null;
        if (!disposed && canConnect()) {
          const delay = Math.min(10_000, 500 * 2 ** Math.min(failures++, 5));
          retry = window.setTimeout(() => void connect(), delay * (0.8 + Math.random() * 0.4));
        }
      }
    };
    const visibilityChanged = () => {
      if (!canConnect()) {
        clearRetry();
        controller?.abort();
        setState((value) => ({ ...value, connected: false }));
      } else {
        failures = 0;
        clearRetry();
        void connect();
      }
    };
    document.addEventListener("visibilitychange", visibilityChanged);
    window.addEventListener("online", visibilityChanged);
    window.addEventListener("offline", visibilityChanged);
    void connect();
    return () => {
      disposed = true;
      clearRetry();
      controller?.abort();
      document.removeEventListener("visibilitychange", visibilityChanged);
      window.removeEventListener("online", visibilityChanged);
      window.removeEventListener("offline", visibilityChanged);
    };
  }, [url]);

  return state;
}
