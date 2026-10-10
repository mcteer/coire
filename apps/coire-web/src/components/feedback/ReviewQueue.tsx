import { useEffect, useRef, useState } from "react";
import {
  getFeedbackReview,
  judgeFeedbackPair,
  listFeedbackReview,
  type FeedbackReviewDetail,
  type ReviewState,
} from "../../api/feedback";

export function ReviewQueue() {
  const [state, setState] = useState<ReviewState>("unreviewed"),
    [revision, setRevision] = useState(0);
  const [rows, setRows] = useState<string[]>([]),
    [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null),
    [detail, setDetail] = useState<FeedbackReviewDetail | null>(null);
  const [error, setError] = useState(""),
    [status, setStatus] = useState(""),
    [tags, setTags] = useState(""),
    [busy, setBusy] = useState(false);
  const requestVersion = useRef(0);
  const decisionIdentity = useRef<{ intent: string; key: string } | null>(null);
  useEffect(() => {
    let live = true;
    setDetail(null);
    setSelected(null);
    setError("");
    listFeedbackReview(state)
      .then((page) => {
        if (live) {
          setRows(page.items.map((pair) => pair.id));
          setCursor(page.next_cursor ?? null);
          setSelected(page.items[0]?.id ?? null);
        }
      })
      .catch((e) => {
        if (live) {
          setRows([]);
          setError(String(e));
        }
      });
    return () => {
      live = false;
    };
  }, [state, revision]);
  useEffect(() => {
    let live = true;
    const version = ++requestVersion.current;
    setDetail(null);
    setTags("");
    if (!selected) return;
    let readSequence = 0,
      initial = true;
    const refresh = () => {
      const read = ++readSequence;
      return getFeedbackReview(selected)
        .then((value) => {
          if (live && version === requestVersion.current && read === readSequence) {
            setDetail(value);
            if (initial) {
              setTags(value.admin_tags?.join(",") ?? "");
              initial = false;
            }
          }
        })
        .catch((e) => {
          if (live && version === requestVersion.current && read === readSequence) {
            setDetail(null);
            setError(`This comparison is unavailable. ${String(e)}`);
          }
        });
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [selected]);
  const decide = async (choice: "original" | "candidate" | "skip") => {
    if (!detail || busy) return;
    const original = detail,
      version = requestVersion.current;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      const body = {
        expected_version: original.admin_judgement?.version ?? 0,
        choice,
        tags: tags
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
      };
      const intent = JSON.stringify({ id: original.id, body });
      if (decisionIdentity.current?.intent !== intent)
        decisionIdentity.current = { intent, key: crypto.randomUUID() };
      await judgeFeedbackPair(original.id, body, decisionIdentity.current.key);
      decisionIdentity.current = null;
      setStatus(
        choice === "skip"
          ? "Skipped for you. Revisit it in Skipped."
          : "Admin judgement saved. The owner's chat answer is unchanged.",
      );
      setRevision((value) => value + 1);
    } catch (e) {
      setError(
        `Decision was not saved. Reloaded the current judgement; review it before trying again. ${String(e)}`,
      );
      try {
        const current = await getFeedbackReview(original.id);
        if (version === requestVersion.current) setDetail(current);
      } catch {
        setDetail(null);
      }
    } finally {
      setBusy(false);
    }
  };
  const older = async () => {
    if (!cursor || busy) return;
    setBusy(true);
    try {
      const page = await listFeedbackReview(state, cursor);
      setRows((old) => [...new Set([...old, ...page.items.map((pair) => pair.id)])]);
      setCursor(page.next_cursor ?? null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-label="Admin pairwise review">
      <h2>Review contributed comparisons</h2>
      <p>
        Only explicitly contributed pairs are shown. Owner withdrawal prevents review and export.
        Your judgement is separate from the owner's choice and does not change their conversation.
      </p>
      <label>
        Review queue
        <select
          disabled={busy}
          value={state}
          onChange={(event) => setState(event.target.value as ReviewState)}
        >
          <option value="unreviewed">Unreviewed</option>
          <option value="reviewed">Reviewed</option>
          <option value="skipped">Skipped</option>
        </select>
      </label>
      {error && <p role="alert">{error}</p>}
      <p role="status">{status}</p>
      <ul>
        {rows.map((id) => (
          <li key={id}>
            <button
              type="button"
              disabled={busy}
              aria-pressed={selected === id}
              onClick={() => setSelected(id)}
            >
              Review {id}
            </button>
          </li>
        ))}
      </ul>
      {!rows.length && <p>No eligible comparisons in this queue.</p>}
      {cursor && (
        <button type="button" disabled={busy} onClick={() => void older()}>
          Load older comparisons
        </button>
      )}
      {detail && (
        <article aria-label="Selected comparison">
          <p>
            Owner {detail.owner_id} · model {detail.target?.model_id} · adapter{" "}
            {detail.target?.adapter_id ?? "bare base"}
          </p>
          <div aria-label="Contributed prompt">
            {(detail.prompt ?? []).map((message, index) => (
              <p key={index}>
                {message.role}: {message.content}
              </p>
            ))}
          </div>
          <section aria-label="Original answer">
            <h3>Original</h3>
            <pre>{detail.original}</pre>
          </section>
          <section aria-label="Candidate answer">
            <h3>Candidate</h3>
            <pre>{detail.candidate}</pre>
          </section>
          <p>
            Owner choice: {detail.owner_judgement?.judgement ?? "No judgement"}. Admin choice:{" "}
            {detail.admin_judgement?.judgement ?? "No judgement"} · version{" "}
            {detail.admin_judgement?.version ?? 0}.
          </p>
          <label>
            Review tags (comma separated)
            <input
              value={tags}
              maxLength={527}
              disabled={busy}
              onChange={(event) => setTags(event.target.value)}
            />
          </label>
          <div className="row">
            <button type="button" disabled={busy} onClick={() => void decide("original")}>
              Prefer original
            </button>
            <button type="button" disabled={busy} onClick={() => void decide("candidate")}>
              Prefer candidate
            </button>
            <button type="button" disabled={busy} onClick={() => void decide("skip")}>
              Skip for now
            </button>
          </div>
        </article>
      )}
    </section>
  );
}
