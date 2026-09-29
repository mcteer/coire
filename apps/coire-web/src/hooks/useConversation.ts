import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { loadChatDrafts, saveChatDrafts } from "../api/chatDrafts";
import {
  createChatConversation,
  getChatConversation,
  listChatModels,
  listChatConversations,
  stopChatTurn,
  type ChatConversation,
  type ChatEvent,
  type ChatMessage,
  type ChatPickerEntry,
  type ChatTurnCreate,
} from "../api/chat";
import { useChatTurnStream } from "./useEventStream";

export function useConversation(ownerId: string) {
  const [initialDrafts] = useState(() => loadChatDrafts(ownerId));
  const drafts = useRef(initialDrafts);
  const [models, setModels] = useState<ChatPickerEntry[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<ChatConversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [history, setHistory] = useState<ChatConversation[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [olderPosition, setOlderPosition] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [draft, setDraft] = useState(() => drafts.current.get("new")?.text ?? "");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [available, setAvailable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [stopPending, setStopPending] = useState(false);
  const busyRef = useRef(false);
  const pending = useRef<{ key: string; body: ChatTurnCreate } | null>(null);
  const selection = useRef(0);
  const stream = useChatTurnStream();

  const remember = (key: string, text: string, modelId: string | null) => {
    drafts.current.delete(key);
    drafts.current.set(key, { text, modelId });
    while (drafts.current.size > 20) {
      const oldest = drafts.current.keys().next().value;
      if (!oldest) break;
      drafts.current.delete(oldest);
    }
    saveChatDrafts(ownerId, drafts.current);
  };

  const forget = (key: string) => {
    drafts.current.delete(key);
    saveChatDrafts(ownerId, drafts.current);
  };

  useEffect(() => {
    let live = true;
    void listChatModels()
      .then((response) => {
        if (!live) return;
        const available = response.data ?? [];
        setModels(available);
        setSelectedId((current) => {
          const saved = drafts.current.get("new")?.modelId;
          return (
            current ??
            (available.some((model) => model.id === saved) ? saved : available[0]?.id) ??
            null
          );
        });
      })
      .catch((cause) => {
        if (live) {
          if (cause instanceof ApiError && cause.status === 404) setAvailable(false);
          else setError(String(cause));
        }
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    let live = true;
    void listChatConversations()
      .then((page) => {
        if (!live) return;
        setHistory(page.data ?? []);
        setHistoryCursor(page.next_cursor ?? null);
      })
      .catch((cause) => {
        if (live) {
          if (cause instanceof ApiError && cause.status === 404) setAvailable(false);
          else setError(String(cause));
        }
      })
      .finally(() => {
        if (live) setHistoryLoading(false);
      });
    return () => {
      live = false;
    };
  }, []);

  const onEvent = (event: ChatEvent, input: string) => {
    const payload = event.payload;
    if (payload.type === "turn.accepted") {
      setConversation(
        (current) =>
          current && {
            ...current,
            revision: payload.turn.accepted_revision + 1,
            selected_model_id: payload.turn.model_id,
            active_turn_id: payload.turn.id,
            updated_at: event.created_at,
          },
      );
      const created = payload.turn.created_at;
      setMessages((current) => {
        if (current.some((message) => message.id === payload.turn.input_message_id)) return current;
        const position = (current.at(-1)?.position ?? 0) + 1;
        return [
          ...current,
          {
            id: payload.turn.input_message_id,
            conversation_id: event.conversation_id,
            position,
            role: "user",
            text: input,
            reasoning: "",
            model_id: payload.turn.model_id,
            model_display_name: payload.turn.model_display_name,
            created_at: created,
            attachment_ids: [],
          },
          {
            id: payload.turn.assistant_message_id,
            conversation_id: event.conversation_id,
            position: position + 1,
            role: "assistant",
            text: "",
            reasoning: "",
            model_id: payload.turn.model_id,
            model_display_name: payload.turn.model_display_name,
            created_at: created,
            attachment_ids: [],
          },
        ];
      });
      setDraft("");
      forget(event.conversation_id);
      pending.current = null;
      setHistory((current) => {
        const item = current.find((row) => row.id === event.conversation_id);
        return item
          ? [
              {
                ...item,
                revision: payload.turn.accepted_revision + 1,
                active_turn_id: payload.turn.id,
                selected_model_id: payload.turn.model_id,
                updated_at: event.created_at,
              },
              ...current.filter((row) => row.id !== item.id),
            ]
          : current;
      });
      setStatus("Preparing response…");
    } else if (payload.type === "turn.status") {
      setStatus(
        payload.state === "loading"
          ? payload.estimate_seconds == null
            ? "Warming up the model · estimate unavailable"
            : "Warming up the model · about " + Math.ceil(payload.estimate_seconds) + " s"
          : payload.state === "queued"
            ? "Waiting for capacity…"
            : payload.state === "stop_requested"
              ? "Stopping response…"
            : payload.state === "running"
              ? "Writing response…"
              : "Preparing response…",
      );
    } else if (payload.type === "message.delta") {
      setMessages((current) =>
        current.map((message) =>
          message.id === payload.message_id
            ? {
                ...message,
                [payload.channel === "answer" ? "text" : "reasoning"]:
                  message[payload.channel === "answer" ? "text" : "reasoning"] + payload.text,
              }
            : message,
        ),
      );
    } else if (payload.type === "turn.terminal") {
      setConversation(
        (current) =>
          current && {
            ...current,
            active_turn_id: null,
            updated_at: event.created_at,
          },
      );
      setHistory((current) =>
        current.map((item) =>
          item.id === event.conversation_id
            ? { ...item, active_turn_id: null, updated_at: event.created_at }
            : item,
        ),
      );
      setStatus(
        payload.state === "completed"
          ? null
          : payload.safe_error || "Response interrupted. Your partial answer was saved.",
      );
    }
  };

  const send = async () => {
    const input = draft.trim();
    if (!input || !selectedId || busyRef.current || stream.active || conversation?.active_turn_id)
      return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    let current = conversation;
    try {
      if (!current) {
        current = await createChatConversation({ mode: "chat", model_id: selectedId });
        setConversation(current);
        setHistory((rows) => [current!, ...rows.filter((row) => row.id !== current!.id)]);
        remember(current.id, input, selectedId);
        forget("new");
      }
      const key = [current.id, current.revision, selectedId, input].join("\0");
      const body: ChatTurnCreate =
        pending.current?.key === key
          ? pending.current.body
          : {
              client_request_id: crypto.randomUUID(),
              expected_revision: current.revision,
              model_id: selectedId,
              content: input,
              action: "chat",
            };
      pending.current = { key, body };
      const generation = selection.current;
      await stream.send(current.id, body, (event) => {
        if (selection.current === generation) onEvent(event, input);
      });
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setError(String(cause));
        setStatus("Send failed. Check the message and try again.");
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const changeDraft = (value: string) => {
    setDraft(value);
    remember(conversation?.id ?? "new", value, selectedId);
  };

  const chooseModel = (id: string) => {
    if (!models.some((model) => model.id === id)) return;
    setSelectedId(id);
    remember(conversation?.id ?? "new", draft, id);
  };

  const openConversation = async (id: string) => {
    if (conversation?.id === id) return;
    if (busyRef.current && !conversation?.active_turn_id) return;
    if (!(await stopOwnedStreamForNavigation())) return;
    remember(conversation?.id ?? "new", draft, selectedId);
    const generation = ++selection.current;
    setHistoryLoading(true);
    setError(null);
    try {
      const detail = await getChatConversation(id);
      if (selection.current !== generation) return;
      setConversation(detail.conversation);
      setMessages(detail.messages ?? []);
      setOlderPosition(detail.next_message_position ?? null);
      const saved = drafts.current.get(id);
      setDraft(saved?.text ?? "");
      const preferred = saved?.modelId ?? detail.conversation.selected_model_id;
      setSelectedId(models.some((model) => model.id === preferred) ? (preferred ?? null) : null);
      setStatus(
        detail.conversation.active_turn_id
          ? "A response is still running. Refresh this conversation to see saved progress."
          : null,
      );
    } catch (cause) {
      if (selection.current === generation) setError(String(cause));
    } finally {
      if (selection.current === generation) setHistoryLoading(false);
    }
  };

  const moreConversations = async () => {
    if (!historyCursor || historyLoading) return;
    setHistoryLoading(true);
    try {
      const page = await listChatConversations(historyCursor);
      setHistory((current) => [
        ...current,
        ...(page.data ?? []).filter((item) => !current.some((known) => known.id === item.id)),
      ]);
      setHistoryCursor(page.next_cursor ?? null);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setHistoryLoading(false);
    }
  };

  const moreMessages = async () => {
    if (!conversation || !olderPosition || historyLoading) return;
    const id = conversation.id;
    const generation = selection.current;
    setHistoryLoading(true);
    try {
      const detail = await getChatConversation(id, olderPosition);
      if (selection.current !== generation) return;
      setMessages((current) => [
        ...(detail.messages ?? []).filter((item) => !current.some((known) => known.id === item.id)),
        ...current,
      ]);
      setOlderPosition(detail.next_message_position ?? null);
    } catch (cause) {
      if (selection.current === generation) setError(String(cause));
    } finally {
      if (selection.current === generation) setHistoryLoading(false);
    }
  };

  const newConversation = async () => {
    if (busyRef.current && !conversation?.active_turn_id) return;
    if (!(await stopOwnedStreamForNavigation())) return;
    remember(conversation?.id ?? "new", draft, selectedId);
    selection.current += 1;
    pending.current = null;
    setConversation(null);
    setMessages([]);
    const saved = drafts.current.get("new");
    setDraft(saved?.text ?? "");
    if (saved?.modelId && models.some((model) => model.id === saved.modelId)) {
      setSelectedId(saved.modelId);
    }
    setOlderPosition(null);
    setStatus(null);
    setError(null);
  };

  const stopOwnedStreamForNavigation = async (): Promise<boolean> => {
    if (!stream.active || !conversation?.active_turn_id) return true;
    try {
      await stopChatTurn(conversation.id, conversation.active_turn_id, "navigation");
      stream.abort();
      return true;
    } catch (cause) {
      setError(String(cause));
      return false;
    }
  };

  const stop = async () => {
    const current = conversation;
    if (!current?.active_turn_id || stopPending) return;
    const generation = selection.current;
    setStopPending(true);
    setError(null);
    try {
      const turn = await stopChatTurn(current.id, current.active_turn_id);
      if (turn.state === "stop_requested") {
        setStatus("Stopping response…");
      } else if (["completed", "failed", "stopped", "interrupted"].includes(turn.state)) {
        const detail = await getChatConversation(current.id);
        if (selection.current === generation) {
          setConversation(detail.conversation);
          setMessages(detail.messages ?? []);
          setStatus(turn.state === "completed" ? null : "Response ended. Your partial answer was saved.");
        }
      }
    } catch (cause) {
      setError(String(cause));
    } finally {
      setStopPending(false);
    }
  };

  return {
    models,
    selectedId,
    setSelectedId: chooseModel,
    conversation,
    messages,
    history,
    historyCursor,
    olderPosition,
    historyLoading,
    draft,
    setDraft: changeDraft,
    status,
    error: error ?? stream.error,
    loading,
    available,
    active: busy || stream.active,
    stopPending,
    stop,
    send,
    newConversation,
    openConversation,
    moreConversations,
    moreMessages,
  };
}
