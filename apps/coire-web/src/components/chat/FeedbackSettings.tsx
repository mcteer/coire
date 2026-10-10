import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  getFeedbackPreference,
  updateFeedbackPreference,
  type FeedbackPreference,
} from "../../api/feedback";

const initialDisclosure =
  "Feedback and comparisons may improve models on this platform. Turning capture off or deleting a conversation removes unexported feedback. Already published training datasets and trained adapters remain unchanged.";

export function FeedbackSettings({
  onChange,
  refreshKey = "",
}: {
  onChange: (preference: FeedbackPreference) => void;
  refreshKey?: string;
}) {
  const [preference, setPreference] = useState<FeedbackPreference | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const callback = useRef(onChange);
  callback.current = onChange;
  const saving = useRef(false);
  const mounted = useRef(false);
  const latest = useRef<FeedbackPreference | null>(null);
  const apply = useCallback((value: FeedbackPreference) => {
    if (
      typeof value?.enabled !== "boolean" ||
      !Number.isSafeInteger(value.version) ||
      value.version < 1 ||
      !Number.isSafeInteger(value.capture_generation) ||
      value.capture_generation < 1 ||
      value.disclosure_version !== "feedback-v1" ||
      !value.disclosure
    )
      throw new Error("Invalid feedback settings response");
    if (latest.current && value.version <= latest.current.version) return;
    latest.current = value;
    setPreference(value);
    callback.current(value);
  }, []);
  useEffect(() => {
    mounted.current = true;
    let current = true;
    void getFeedbackPreference()
      .then((value) => {
        if (!current) return;
        apply(value);
      })
      .catch(() => {
        if (current) setError("Feedback settings are unavailable. You can continue chatting.");
      });
    return () => {
      current = false;
      mounted.current = false;
    };
  }, [refreshKey, apply]);

  const toggle = async () => {
    if (!preference || saving.current) return;
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      const updated = await updateFeedbackPreference({
        client_request_id: crypto.randomUUID(),
        expected_version: preference.version,
        enabled: !preference.enabled,
        disclosure_version: preference.disclosure_version,
      });
      if (!mounted.current) return;
      apply(updated);
    } catch (cause) {
      if (!mounted.current) return;
      if (cause instanceof ApiError && cause.status === 409) {
        try {
          const updated = await getFeedbackPreference();
          if (!mounted.current) return;
          apply(updated);
        } catch {
          /* Keep the last state while clearly reporting the conflict. */
        }
        if (mounted.current)
          setError("Feedback settings changed. Review the current state before trying again.");
      } else setError("Could not save feedback settings. Your current setting is unchanged.");
    } finally {
      saving.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  return (
    <section className="chat-feedback-settings glass" aria-label="Feedback privacy">
      <h2>Feedback privacy</h2>
      <p id="feedback-disclosure">{preference?.disclosure ?? initialDisclosure}</p>
      {preference ? (
        <>
          <label>
            <input
              type="checkbox"
              checked={preference.enabled}
              disabled={busy}
              aria-describedby="feedback-disclosure"
              onChange={() => void toggle()}
            />{" "}
            Capture feedback
          </label>
          <p role="status" aria-live="polite">
            {busy
              ? "Saving feedback settings…"
              : `Feedback capture is ${preference.enabled ? "on" : "off"}.`}
          </p>
        </>
      ) : !error ? (
        <p role="status">Loading feedback settings…</p>
      ) : null}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
