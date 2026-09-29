import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { loadChatDrafts, saveChatDrafts } from "../api/chatDrafts";
import {
  createChatConversation,
  deleteChatConversation,
  getChatConversation,
  getChatFile,
  listChatModels,
  listChatConversations,
  processChatFile,
  stopChatTurn,
  updateChatConversation,
  uploadChatFile,
  type ChatAttachment,
  type ChatAttachmentSelection,
  type ChatConversation,
  type ChatEvent,
  type ChatMessage,
  type ChatPickerEntry,
  type ChatTurnCreate,
  type ChatTurn,
} from "../api/chat";
import { useChatConversationObserver, useChatTurnStream } from "./useEventStream";

export function useConversation(ownerId: string) {
  const [initialDrafts] = useState(() => loadChatDrafts(ownerId));
  const drafts = useRef(initialDrafts);
  const [models, setModels] = useState<ChatPickerEntry[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<ChatConversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [selections, setSelections] = useState<ChatAttachmentSelection[]>([]);
  const [fileBusy, setFileBusy] = useState(false);
  const canSendSelections = selections.every((item) => {
    const attachment = attachments.find((row) => row.id === item.file_id);
    return (
      item.mode === "text" &&
      attachment?.state === "ready" &&
      !attachment.detected_type.startsWith("image/")
    );
  });
  const latestTurn = turns.at(-1);
  const retryableTurn =
    latestTurn &&
    ["failed", "stopped", "interrupted"].includes(latestTurn.state) &&
    messages.at(-1)?.id === latestTurn.assistant_message_id &&
    !conversation?.active_turn_id
      ? latestTurn
      : null;
  const [history, setHistory] = useState<ChatConversation[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [olderPosition, setOlderPosition] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [draft, setDraft] = useState(() => drafts.current.get("new")?.text ?? "");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reauthRequired, setReauthRequired] = useState(false);
  const [loading, setLoading] = useState(true);
  const [available, setAvailable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [stopPending, setStopPending] = useState(false);
  const busyRef = useRef(false);
  const pending = useRef<{ key: string; body: ChatTurnCreate } | null>(null);
  const selection = useRef(0);
  const eventCursor = useRef(0);
  const refreshTimer = useRef<number | null>(null);
  const refreshDirty = useRef(false);
  const stream = useChatTurnStream();

  const reportError = (cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 401) {
      setReauthRequired(true);
      setError("Your session expired. Sign in again to continue this conversation.");
    } else {
      setError(String(cause));
    }
  };

  const remember = (
    key: string,
    text: string,
    modelId: string | null,
    files: ChatAttachmentSelection[] = selections,
  ) => {
    drafts.current.delete(key);
    drafts.current.set(key, { text, modelId, ...(key !== "new" && files.length ? { files } : {}) });
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
          else reportError(cause);
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
          else reportError(cause);
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
    eventCursor.current = Math.max(eventCursor.current, event.cursor);
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
      const selectedFiles = pending.current?.body.attachments ?? [];
      setTurns((current) => [...current.filter((row) => row.id !== payload.turn.id), payload.turn]);
      setMessages((current) => {
        const hasInput = current.some((message) => message.id === payload.turn.input_message_id);
        const hasAssistant = current.some(
          (message) => message.id === payload.turn.assistant_message_id,
        );
        const position = (current.at(-1)?.position ?? 0) + 1;
        return [
          ...current,
          ...(!hasInput
            ? [
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
                  attachment_ids: selectedFiles.map((item) => item.file_id),
                  attachment_selections: selectedFiles,
                } satisfies ChatMessage,
              ]
            : []),
          ...(!hasAssistant
            ? [
                {
                  id: payload.turn.assistant_message_id,
                  conversation_id: event.conversation_id,
                  position: position + (hasInput ? 0 : 1),
                  role: "assistant",
                  text: "",
                  reasoning: "",
                  model_id: payload.turn.model_id,
                  model_display_name: payload.turn.model_display_name,
                  created_at: created,
                  attachment_ids: [],
                } satisfies ChatMessage,
              ]
            : []),
        ];
      });
      if (!payload.turn.retry_of) {
        setDraft("");
        setSelections([]);
        forget(event.conversation_id);
      }
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
      setTurns((current) =>
        current.map((turn) =>
          turn.id === event.turn_id ? { ...turn, state: payload.state } : turn,
        ),
      );
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
    if (
      !input ||
      !selectedId ||
      !canSendSelections ||
      fileBusy ||
      busyRef.current ||
      stream.active ||
      conversation?.active_turn_id
    )
      return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    let current = conversation;
    try {
      if (!current) {
        current = await createChatConversation({ mode: "chat", model_id: selectedId });
        eventCursor.current = 0;
        setConversation(current);
        setHistory((rows) => [current!, ...rows.filter((row) => row.id !== current!.id)]);
        remember(current.id, input, selectedId);
        forget("new");
      }
      const key = [
        current.id,
        current.revision,
        selectedId,
        input,
        JSON.stringify(selections),
      ].join("\0");
      const body: ChatTurnCreate =
        pending.current?.key === key
          ? pending.current.body
          : {
              client_request_id: crypto.randomUUID(),
              expected_revision: current.revision,
              model_id: selectedId,
              content: input,
              action: "chat",
              attachments: selections,
            };
      pending.current = { key, body };
      const generation = selection.current;
      await stream.send(current.id, body, (event) => {
        if (selection.current === generation) onEvent(event, input);
      });
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        reportError(cause);
        setStatus("Send failed. Check the message and try again.");
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const retry = async () => {
    const current = conversation;
    const previous = retryableTurn;
    const input = messages.find((message) => message.id === previous?.input_message_id);
    if (
      !current ||
      !previous ||
      !input ||
      !selectedId ||
      busyRef.current ||
      stream.active ||
      fileBusy
    )
      return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const key = [current.id, current.revision, selectedId, previous.id, "retry"].join("\0");
    const body: ChatTurnCreate =
      pending.current?.key === key
        ? pending.current.body
        : {
            client_request_id: crypto.randomUUID(),
            expected_revision: current.revision,
            model_id: selectedId,
            content: input.text,
            attachments: input.attachment_selections ?? [],
            action: "chat",
            retry_of: previous.id,
          };
    pending.current = { key, body };
    const generation = selection.current;
    try {
      await stream.send(current.id, body, (event) => {
        if (selection.current === generation) onEvent(event, input.text);
      });
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        reportError(cause);
        setStatus("Retry failed. Review the response and try again.");
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

  const refreshFiles = async (id: string, generation: number) => {
    const detail = await getChatConversation(id);
    if (selection.current !== generation) return;
    setConversation(detail.conversation);
    setAttachments(detail.attachments ?? []);
  };

  const uploadFile = async (file: File) => {
    if (fileBusy || busyRef.current || stream.active || conversation?.active_turn_id || !selectedId)
      return;
    setFileBusy(true);
    setError(null);
    const generation = selection.current;
    let current = conversation;
    try {
      if (!current) {
        current = await createChatConversation({ mode: "chat", model_id: selectedId });
        if (selection.current !== generation) return;
        setConversation(current);
        setHistory((rows) => [current!, ...rows.filter((row) => row.id !== current!.id)]);
        remember(current.id, draft, selectedId);
        forget("new");
      }
      const uploaded = await uploadChatFile(current.id, file, current.revision);
      if (selection.current !== generation) return;
      setAttachments((rows) => [...rows.filter((row) => row.id !== uploaded.id), uploaded]);
      await refreshFiles(current.id, generation);
    } catch (cause) {
      if (selection.current === generation) {
        reportError(cause);
        if (current) await refreshFiles(current.id, generation).catch(() => {});
      }
    } finally {
      setFileBusy(false);
    }
  };

  const processFile = async (fileId: string, operation: "inspect" | "render", pages?: number[]) => {
    const current = conversation;
    if (!current || fileBusy || busyRef.current || stream.active || current.active_turn_id) return;
    setFileBusy(true);
    setError(null);
    const generation = selection.current;
    try {
      const updated = await processChatFile(current.id, fileId, {
        request_id: crypto.randomUUID(),
        expected_revision: current.revision,
        operation,
        selected_pages: pages ?? [],
      });
      if (selection.current !== generation) return;
      setAttachments((rows) => rows.map((row) => (row.id === fileId ? updated : row)));
      await refreshFiles(current.id, generation);
    } catch (cause) {
      if (selection.current === generation) {
        reportError(cause);
        await refreshFiles(current.id, generation).catch(() => {});
      }
    } finally {
      setFileBusy(false);
    }
  };

  const selectFile = (value: ChatAttachmentSelection | null, fileId: string) => {
    const next = [...selections.filter((row) => row.file_id !== fileId), ...(value ? [value] : [])];
    setSelections(next);
    remember(conversation?.id ?? "new", draft, selectedId, next);
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
      eventCursor.current = detail.event_cursor;
      setConversation(detail.conversation);
      setMessages(detail.messages ?? []);
      setTurns(detail.turns ?? []);
      setAttachments(detail.attachments ?? []);
      setOlderPosition(detail.next_message_position ?? null);
      const saved = drafts.current.get(id);
      const restored = (saved?.files ?? []).filter((item) => {
        const file = (detail.attachments ?? []).find((row) => row.id === item.file_id);
        const pageCount = file?.page_count;
        return (
          file &&
          file.state !== "deleting" &&
          (!item.pages?.length ||
            (pageCount !== null &&
              pageCount !== undefined &&
              item.pages.every((page) => page <= pageCount)))
        );
      });
      setSelections(restored);
      setDraft(saved?.text ?? "");
      const preferred = saved?.modelId ?? detail.conversation.selected_model_id;
      setSelectedId(models.some((model) => model.id === preferred) ? (preferred ?? null) : null);
      setStatus(
        restored.length !== (saved?.files?.length ?? 0)
          ? "Some saved file choices are no longer available. Check the selection before sending."
          : detail.conversation.active_turn_id
            ? "A response is still running. Refresh this conversation to see saved progress."
            : null,
      );
    } catch (cause) {
      if (selection.current === generation) reportError(cause);
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
      reportError(cause);
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
      if (selection.current === generation) reportError(cause);
    } finally {
      if (selection.current === generation) setHistoryLoading(false);
    }
  };

  const newConversation = async () => {
    if (busyRef.current && !conversation?.active_turn_id) return;
    if (!(await stopOwnedStreamForNavigation())) return;
    remember(conversation?.id ?? "new", draft, selectedId);
    selection.current += 1;
    eventCursor.current = 0;
    pending.current = null;
    setConversation(null);
    setMessages([]);
    setTurns([]);
    setAttachments([]);
    setSelections([]);
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
      reportError(cause);
      return false;
    }
  };

  const refreshObserved = (event: ChatEvent) => {
    if (event.conversation_id !== conversation?.id) return;
    if (event.payload.type === "conversation.deleted") {
      selection.current += 1;
      setHistory((rows) => rows.filter((row) => row.id !== event.conversation_id));
      forget(event.conversation_id);
      setConversation(null);
      setMessages([]);
      setTurns([]);
      setAttachments([]);
      setSelections([]);
      setOlderPosition(null);
      setDraft(drafts.current.get("new")?.text ?? "");
      setStatus("Conversation deleted.");
      return;
    }
    if (event.payload.type === "snapshot") {
      const detail = event.payload.detail;
      setConversation(detail.conversation);
      setMessages(detail.messages ?? []);
      setTurns(detail.turns ?? []);
      setAttachments(detail.attachments ?? []);
      setOlderPosition(detail.next_message_position ?? null);
      setHistory((rows) =>
        rows.map((row) => (row.id === event.conversation_id ? detail.conversation : row)),
      );
      setStatus(detail.conversation.active_turn_id ? "A response is still running." : null);
      return;
    }
    if (refreshTimer.current !== null) {
      refreshDirty.current = true;
      return;
    }
    const id = event.conversation_id;
    const generation = selection.current;
    const refresh = () => {
      refreshTimer.current = window.setTimeout(() => {
        void getChatConversation(id)
          .then((detail) => {
            if (selection.current !== generation) return;
            setConversation(detail.conversation);
            setMessages(detail.messages ?? []);
            setTurns(detail.turns ?? []);
            setAttachments(detail.attachments ?? []);
            setOlderPosition(detail.next_message_position ?? null);
            setHistory((rows) => rows.map((row) => (row.id === id ? detail.conversation : row)));
            setStatus(detail.conversation.active_turn_id ? "A response is still running." : null);
          })
          .catch((cause) => {
            if (selection.current === generation) reportError(cause);
          })
          .finally(() => {
            refreshTimer.current = null;
            if (refreshDirty.current && selection.current === generation) {
              refreshDirty.current = false;
              refresh();
            }
          });
      }, 250);
    };
    refresh();
  };

  useChatConversationObserver(
    conversation?.id ?? null,
    !stream.active,
    eventCursor,
    refreshObserved,
    (status) => {
      if (status === 401) reportError(new ApiError(401, "Session expired"));
    },
  );

  useEffect(() => {
    const id = conversation?.id;
    if (!id || !attachments.some((item) => item.state === "processing")) return;
    const generation = selection.current;
    const timer = window.setInterval(() => {
      for (const item of attachments) {
        if (item.state !== "processing") continue;
        void getChatFile(id, item.id)
          .then((updated) => {
            if (selection.current !== generation) return;
            setAttachments((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
          })
          .catch((cause) => {
            if (selection.current === generation) reportError(cause);
          });
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [conversation?.id, attachments]);

  useEffect(
    () => () => {
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
    },
    [],
  );

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
          setTurns(detail.turns ?? []);
          setStatus(
            turn.state === "completed" ? null : "Response ended. Your partial answer was saved.",
          );
        }
      }
    } catch (cause) {
      reportError(cause);
    } finally {
      setStopPending(false);
    }
  };

  const rename = async (id: string, title: string, expectedRevision: number): Promise<boolean> => {
    try {
      const updated = await updateChatConversation(id, {
        expected_revision: expectedRevision,
        title: title.trim(),
      });
      setHistory((rows) => rows.map((row) => (row.id === id ? updated : row)));
      setConversation((current) => (current?.id === id ? updated : current));
      setError(null);
      return true;
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        try {
          const page = await listChatConversations();
          setHistory(page.data ?? []);
          setHistoryCursor(page.next_cursor ?? null);
        } catch {
          // Preserve the original edit conflict for the user.
        }
      }
      reportError(cause);
      return false;
    }
  };

  const remove = async (id: string, expectedRevision: number): Promise<boolean> => {
    try {
      await deleteChatConversation(id, { expected_revision: expectedRevision });
      setHistory((rows) => rows.filter((row) => row.id !== id));
      forget(id);
      if (conversation?.id === id) {
        selection.current += 1;
        stream.abort();
        eventCursor.current = 0;
        pending.current = null;
        setConversation(null);
        setMessages([]);
        setTurns([]);
        setAttachments([]);
        setSelections([]);
        setOlderPosition(null);
        setDraft(drafts.current.get("new")?.text ?? "");
        setStatus(null);
      }
      setError(null);
      return true;
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        try {
          const page = await listChatConversations();
          setHistory(page.data ?? []);
          setHistoryCursor(page.next_cursor ?? null);
        } catch {
          // Preserve the deletion conflict for the user.
        }
      }
      reportError(cause);
      return false;
    }
  };

  return {
    models,
    selectedId,
    setSelectedId: chooseModel,
    conversation,
    messages,
    retryableTurn,
    retry,
    attachments,
    selections,
    canSendSelections,
    fileBusy,
    uploadFile,
    processFile,
    selectFile,
    history,
    historyCursor,
    olderPosition,
    historyLoading,
    draft,
    setDraft: changeDraft,
    status,
    error: error ?? stream.error,
    reauthRequired,
    loading,
    available,
    active: busy || stream.active,
    stopPending,
    stop,
    rename,
    remove,
    send,
    newConversation,
    openConversation,
    moreConversations,
    moreMessages,
  };
}
