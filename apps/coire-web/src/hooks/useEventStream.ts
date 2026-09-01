import { useEffect, useRef, useState } from "react";
type State<T> = { data: T | null; connected: boolean; error: string | null };
export function useEventStream<T>(url: string, initial: T | null = null): State<T> {
  const [state, setState] = useState<State<T>>({ data: initial, connected: false, error: null });
  const lastId = useRef<string | null>(null);
  useEffect(() => {
    if (url === "") return;
    const controller = new AbortController();
    let retry: number | undefined;
    const connect = async () => {
      try {
        const response = await fetch(url, {
          credentials: "same-origin",
          headers: lastId.current ? { "Last-Event-ID": lastId.current } : {},
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`stream refused (${response.status})`);
        setState((v) => ({ ...v, connected: true, error: null }));
        const reader = response.body.getReader(),
          decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const blocks = buffer.split("\n\n");
          buffer = blocks.pop() ?? "";
          for (const block of blocks) {
            const id = block.match(/^id: (.+)$/m)?.[1],
              payload = block.match(/^data: (.+)$/m)?.[1];
            if (!payload) continue;
            if (id) lastId.current = id;
            const event = JSON.parse(payload) as { snapshot: T };
            setState({ data: event.snapshot, connected: true, error: null });
          }
        }
        throw new Error("stream ended");
      } catch (error) {
        if (controller.signal.aborted) return;
        setState((v) => ({ ...v, connected: false, error: String(error) }));
        retry = window.setTimeout(() => void connect(), 1000);
      }
    };
    void connect();
    return () => {
      controller.abort();
      if (retry) window.clearTimeout(retry);
    };
  }, [url]);
  return state;
}
