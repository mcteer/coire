import { useEffect, useRef, useState } from "react";
import {
  createChatConversation,
  listChatModels,
  type ChatConversation,
  type ChatEvent,
  type ChatMessage,
  type ChatPickerEntry,
  type ChatTurnCreate,
} from "../api/chat";
import { useChatTurnStream } from "./useEventStream";

export function useConversation() {
  const [models, setModels] = useState<ChatPickerEntry[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<ChatConversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const pending = useRef<{ key: string; body: ChatTurnCreate } | null>(null);
  const stream = useChatTurnStream();

  useEffect(() => {
    let live = true;
    void listChatModels()
      .then((response) => {
        if (!live) return;
        const available = response.data ?? [];
        setModels(available);
        setSelectedId((current) => current ?? available[0]?.id ?? null);
      })
      .catch((cause) => {
        if (live) setError(String(cause));
      })
      .finally(() => {
        if (live) setLoading(false);
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
          },
      );
      const created = payload.turn.created_at;
      setMessages((current) => {
        if (current.some((message) => message.id === payload.turn.input_message_id)) return current;
        const position = current.length + 1;
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
      pending.current = null;
      setStatus("Preparing response…");
    } else if (payload.type === "turn.status") {
      setStatus(
        payload.state === "loading"
          ? payload.estimate_seconds == null
            ? "Warming up the model · estimate unavailable"
            : "Warming up the model · about " + Math.ceil(payload.estimate_seconds) + " s"
          : payload.state === "queued"
            ? "Waiting for capacity…"
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
      setConversation((current) => current && { ...current, active_turn_id: null });
      setStatus(
        payload.state === "completed"
          ? null
          : payload.safe_error || "Response interrupted. Your partial answer was saved.",
      );
    }
  };

  const send = async () => {
    const input = draft.trim();
    if (!input || !selectedId || busyRef.current || stream.active) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    let current = conversation;
    try {
      if (!current) {
        current = await createChatConversation({ mode: "chat", model_id: selectedId });
        setConversation(current);
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
      await stream.send(current.id, body, (event) => onEvent(event, input));
    } catch (cause) {
      setError(String(cause));
      setStatus("Send failed. Check the message and try again.");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const newConversation = () => {
    if (busyRef.current || stream.active) return;
    pending.current = null;
    setConversation(null);
    setMessages([]);
    setDraft("");
    setStatus(null);
    setError(null);
  };

  return {
    models,
    selectedId,
    setSelectedId,
    conversation,
    messages,
    draft,
    setDraft,
    status,
    error: error ?? stream.error,
    loading,
    active: busy || stream.active,
    send,
    newConversation,
  };
}
