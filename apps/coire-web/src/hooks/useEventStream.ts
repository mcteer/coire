import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import { openChatEvents, sendChatTurn, type ChatEvent, type ChatTurnCreate } from "../api/chat";
import { openEventStream, readEventStream } from "../api/eventStream";

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
        await readEventStream(response, active.signal, (frame) => {
          const event = JSON.parse(frame.data) as { snapshot: T };
          if (frame.id) lastId.current = frame.id;
          failures = 0;
          setState({ data: event.snapshot, connected: true, error: null });
        });
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

/** A native generation is owned by one explicit send, including in hidden tabs. */
export function useChatTurnStream() {
  const controller = useRef<AbortController | null>(null);
  const [active, setActive] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const send = useCallback(
    async (
      conversationId: string,
      body: ChatTurnCreate,
      onEvent: (event: ChatEvent) => void,
    ): Promise<void> => {
      if (controller.current) throw new Error("chat turn already active");
      const current = new AbortController();
      controller.current = current;
      setActive(true);
      setError(null);
      try {
        await sendChatTurn(conversationId, body, current.signal, onEvent);
      } catch (cause) {
        if (!current.signal.aborted) setError(String(cause));
        throw cause;
      } finally {
        if (controller.current === current) controller.current = null;
        setActive(false);
      }
    },
    [],
  );

  const abort = useCallback(() => controller.current?.abort(), []);
  return { active, error, send, abort };
}

/** A selected conversation is observed with GET only; it never replays its POST. */
export function useChatConversationObserver(
  conversationId: string | null,
  enabled: boolean,
  cursor: MutableRefObject<number>,
  onEvent: (event: ChatEvent) => void,
) {
  const callback = useRef(onEvent);
  useEffect(() => {
    callback.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    if (!conversationId || !enabled) return;
    let disposed = false;
    let controller: AbortController | null = null;
    let retry: number | undefined;
    let failures = 0;
    let permanent = false;
    const connect = async () => {
      if (disposed || controller) return;
      const active = new AbortController();
      controller = active;
      try {
        const response = await openChatEvents(conversationId, cursor.current, active.signal);
        if ([401, 403, 404].includes(response.status)) {
          permanent = true;
          return;
        }
        if (response.status === 409) cursor.current = 0;
        if (!response.ok || !response.headers.get("content-type")?.startsWith("text/event-stream"))
          throw new Error(`chat observer refused (${response.status})`);
        await readEventStream(response, active.signal, (frame) => {
          const parsed: unknown = JSON.parse(frame.data);
          if (!parsed || typeof parsed !== "object") throw new Error("invalid chat event");
          const event = parsed as ChatEvent;
          if (
            event.conversation_id !== conversationId ||
            !Number.isSafeInteger(event.cursor) ||
            event.cursor <= cursor.current ||
            frame.id !== `${conversationId}:${event.cursor}` ||
            frame.event !== event.payload?.type ||
            (event.payload.type !== "snapshot" && event.cursor !== cursor.current + 1)
          )
            throw new Error("invalid chat event");
          cursor.current = event.cursor;
          failures = 0;
          callback.current(event);
        });
      } catch {
        // The selected conversation remains readable from its saved detail while reconnecting.
      } finally {
        if (controller === active) controller = null;
        if (!disposed && !permanent) {
          retry = window.setTimeout(() => void connect(), Math.min(500 * 2 ** failures++, 5000));
        }
      }
    };
    retry = window.setTimeout(() => void connect(), 500);
    return () => {
      disposed = true;
      if (retry !== undefined) window.clearTimeout(retry);
      controller?.abort();
    };
  }, [conversationId, enabled, cursor]);
}
