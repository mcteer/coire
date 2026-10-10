import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  dismissComparison,
  getComparison,
  selectComparison,
  type ComparisonDetail,
  type ComparisonReceipt,
  type ComparisonSelect,
} from "../../api/feedback";

export function Comparison({
  conversationId,
  receipt,
  enabled,
  onChange,
}: {
  conversationId: string;
  receipt: ComparisonReceipt;
  enabled: boolean;
  onChange: () => void | Promise<void>;
}) {
  const [detail, setDetail] = useState<ComparisonDetail | null>(null);
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saving = useRef(false);
  useEffect(() => {
    let current = true;
    if (!enabled) {
      setDetail(null);
      return;
    }
    void getComparison(conversationId, receipt.id)
      .then((value) => {
        if (current) {
          setDetail(value);
          setTags((value.owner_tags ?? []).join(", "));
          setError(null);
        }
      })
      .catch(() => {
        if (current) setError("Could not refresh this comparison.");
      });
    return () => {
      current = false;
    };
  }, [
    conversationId,
    receipt.id,
    receipt.version,
    receipt.state,
    receipt.selection_state,
    enabled,
  ]);
  const choose = async (candidate: ComparisonSelect["candidate"]) => {
    if (!detail || !enabled || busy || saving.current) return;
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
      const result = await selectComparison(conversationId, receipt.id, {
        client_request_id: crypto.randomUUID(),
        expected_version: detail.version,
        candidate,
        tags: selectedTags,
      });
      setDetail({
        ...detail,
        ...result.comparison,
        selected: detail.selected ?? candidate,
        owner_judgement: result.feedback,
        owner_tags: selectedTags,
      });
      await onChange();
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.status === 409
          ? "Comparison changed. Review the current state before choosing."
          : "Could not save this choice.",
      );
      try {
        setDetail(await getComparison(conversationId, receipt.id));
      } catch {
        /* Preserve the visible refusal. */
      }
      await Promise.resolve(onChange()).catch(() => {});
    } finally {
      saving.current = false;
      setBusy(false);
    }
  };
  const dismiss = async () => {
    if (!detail || !enabled || busy || saving.current) return;
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await dismissComparison(conversationId, receipt.id, {
        client_request_id: crypto.randomUUID(),
        expected_version: detail.version,
      });
      setDetail({ ...detail, ...result, original: null, candidate: null });
      await onChange();
    } catch {
      setError("Comparison changed or could not be dismissed. Refresh before trying again.");
      await Promise.resolve(onChange()).catch(() => {});
    } finally {
      saving.current = false;
      setBusy(false);
    }
  };
  if (!enabled) return <p role="status">Comparison contribution was withdrawn.</p>;
  const pending = detail?.selection_state === "pending";
  const visible = detail?.eligibility === "eligible";
  const selectable =
    visible && detail.state === "ready" && (pending || detail.selection_state === "chosen");
  return (
    <section className="chat-comparison glass" aria-label="Answer comparison">
      <h2>Answer comparison</h2>
      <p role="status" aria-live="polite">
        {!detail
          ? "Loading comparison…"
          : busy
            ? "Saving your choice…"
            : detail.state === "queued" || detail.state === "running"
              ? "Generating another answer…"
              : detail.state === "identical"
                ? "The answers were identical. Your original answer remains active."
                : detail.state === "failed"
                  ? "Another answer could not be generated. Your original answer remains active."
                  : detail.selection_state === "chosen"
                    ? `${detail.selected === "candidate" ? "The alternative" : "The original"} answer is active in this conversation.`
                    : detail.selection_state === "pending"
                      ? "Choose an answer or dismiss this comparison before sending another message."
                      : "This comparison is closed."}
      </p>
      {visible && (
        <div className="chat-comparison-answers">
          <div>
            <h3>Original answer</h3>
            <div className="chat-comparison-text">{detail.original}</div>
          </div>
          <div>
            <h3>Alternative answer</h3>
            <div className="chat-comparison-text">
              {detail.candidate ?? "Waiting for the answer…"}
            </div>
          </div>
        </div>
      )}
      {selectable && (
        <>
          <p>
            {pending
              ? "Your original answer stays active until you choose. Only the selected answer enters future context."
              : "Changing your feedback does not change the answer already selected for this conversation."}
          </p>
          <div className="row">
            <label>
              Comparison tags
              <input
                value={tags}
                maxLength={544}
                disabled={busy}
                placeholder="clear, accurate"
                onChange={(event) => setTags(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="button"
              disabled={busy}
              aria-pressed={detail.owner_judgement?.judgement === "original"}
              onClick={() => void choose("original")}
            >
              {pending ? "Choose original answer" : "Prefer original answer"}
            </button>
            <button
              type="button"
              className="button"
              disabled={busy}
              aria-pressed={detail.owner_judgement?.judgement === "candidate"}
              onClick={() => void choose("candidate")}
            >
              {pending ? "Choose alternative answer" : "Prefer alternative answer"}
            </button>
          </div>
        </>
      )}
      {pending && visible && (
        <button type="button" className="button" disabled={busy} onClick={() => void dismiss()}>
          Dismiss comparison
        </button>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
