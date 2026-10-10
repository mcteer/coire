import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  createComparison,
  updateThumb,
  type ComparisonReceipt,
  type MessageFeedback,
} from "../../api/feedback";

export function FeedbackControls({
  conversationId,
  revision,
  row,
  enabled,
  complete,
  pending = false,
  onChange,
  onCreated,
}: {
  conversationId: string;
  revision: number;
  row: MessageFeedback;
  enabled: boolean;
  complete: boolean;
  pending?: boolean;
  onChange: () => void | Promise<void>;
  onCreated: (receipt: ComparisonReceipt) => void;
}) {
  const [current, setCurrent] = useState(row.feedback ?? null);
  const [tags, setTags] = useState((row.tags ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saving = useRef(false);
  useEffect(() => {
    setCurrent(row.feedback ?? null);
    setTags((row.tags ?? []).join(", "));
  }, [row]);
  const disabled = !enabled || !complete || busy || row.eligibility === "capture_disabled";
  const vote = async (judgement: "up" | "down") => {
    if (disabled || saving.current) return;
    const selectedTags = tags
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    if (
      selectedTags.length > 16 ||
      new Set(selectedTags).size !== selectedTags.length ||
      selectedTags.some((value) => !/^[a-z0-9][a-z0-9-]{0,31}$/.test(value))
    ) {
      setError("Use up to 16 different tags with lowercase letters, numbers or hyphens.");
      return;
    }
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      const updated = await updateThumb(conversationId, row.message_id, {
        client_request_id: crypto.randomUUID(),
        expected_version: current?.version ?? 0,
        judgement: current?.judgement === judgement ? null : judgement,
        tags: selectedTags,
      });
      setCurrent(updated);
      await onChange();
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 409
          ? "Feedback changed. Review the current choice before trying again."
          : "Could not save feedback.",
      );
      await Promise.resolve(onChange()).catch(() => {});
    } finally {
      saving.current = false;
      setBusy(false);
    }
  };
  const compare = async () => {
    if (disabled || pending || row.eligibility !== "eligible" || saving.current) return;
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      onCreated(
        await createComparison(conversationId, {
          client_request_id: crypto.randomUUID(),
          expected_revision: revision,
          source_message_id: row.message_id,
        }),
      );
      await onChange();
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 409
          ? "Conversation changed. Review the current state before comparing."
          : "Another answer could not be generated. Your original answer is still active.",
      );
      await Promise.resolve(onChange()).catch(() => {});
    } finally {
      saving.current = false;
      setBusy(false);
    }
  };
  return (
    <div className="chat-feedback-controls" role="group" aria-label="Answer feedback">
      <button
        type="button"
        className="button"
        aria-pressed={current?.judgement === "up"}
        disabled={disabled}
        onClick={() => void vote("up")}
      >
        Helpful answer
      </button>
      <button
        type="button"
        className="button"
        aria-pressed={current?.judgement === "down"}
        disabled={disabled}
        onClick={() => void vote("down")}
      >
        Unhelpful answer
      </button>
      <label>
        Feedback tags
        <input
          value={tags}
          maxLength={544}
          disabled={disabled}
          placeholder="clear, accurate"
          onChange={(event) => setTags(event.target.value)}
        />
      </label>
      <button
        type="button"
        className="button"
        disabled={disabled || pending || row.eligibility !== "eligible"}
        onClick={() => void compare()}
      >
        Compare another answer
      </button>
      <span role="status" aria-live="polite">
        {busy
          ? "Saving feedback…"
          : current?.judgement
            ? "Feedback saved. Select the same rating to clear it."
            : ""}
      </span>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
