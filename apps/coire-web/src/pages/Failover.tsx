import { useEffect, useState } from "react";
import {
  readFailoverStatus,
  readResidentModels,
  streamFailoverCompletion,
  type CompletionRequest,
  type FailoverStatus,
  type ResidentModel,
} from "../api/failover";

const TIER_LABEL: Record<FailoverStatus["tier"], string> = {
  full: "Full service",
  degraded_inference: "Degraded inference",
  minimal: "Minimal single-node inference",
};

export function FailoverPage({
  status,
  models = [],
}: {
  status?: FailoverStatus;
  models?: ResidentModel[];
}) {
  const [live, setLive] = useState<FailoverStatus | null>(status ?? null);
  const [listed, setListed] = useState<ResidentModel[]>(models);
  const [selected, setSelected] = useState(models[0]?.id ?? "");
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<CompletionRequest["messages"]>([]);
  const [responseText, setResponseText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (status) {
      return;
    }
    void readFailoverStatus()
      .then(setLive)
      .catch(() =>
        setLive({
          tier: "minimal",
          elected_host: null,
          in_flight: 0,
          unavailable_capabilities: ["conversation_persistence"],
        }),
      );
    void readResidentModels()
      .then((data) => {
        setListed(data);
        setSelected((prior) => prior || data[0]?.id || "");
      })
      .catch(() => setListed([]));
  }, [status]);

  const tier = live?.tier ?? "minimal";
  return (
    <main className="app">
      <section className="panel glass" aria-label="Degraded mode">
        <p className="error banner" role="status">
          Coire is in degraded mode. Nothing you send is persisted — there is no conversation
          history, usage record, or feedback.
        </p>
        <h1>{TIER_LABEL[tier]}</h1>
        <p>
          {live?.elected_host
            ? `Served by ${live.elected_host}.`
            : "No elected frontend is currently serving."}
        </p>
        {tier === "minimal" ? (
          <p>Sharded inference is unavailable. Only models resident on this Studio can run.</p>
        ) : null}
        <h2>Unavailable</h2>
        <ul>
          {(live?.unavailable_capabilities ?? ["conversation_persistence"]).map((capability) => (
            <li key={capability}>{capability.replaceAll("_", " ")}</li>
          ))}
        </ul>
        <h2>Resident models</h2>
        {listed.length === 0 ? (
          <p>No resident model is available.</p>
        ) : (
          <ul>
            {listed.map((model) => (
              <li key={model.id}>{model.coire_description || model.id}</li>
            ))}
          </ul>
        )}
        {listed.length > 0 ? (
          <label>
            Resident model
            <select value={selected} onChange={(event) => setSelected(event.target.value)}>
              {listed.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.coire_description || model.id}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {messages.map((message, index) => (
          <p key={index}>
            <strong>{message.role}:</strong> {message.content}
          </p>
        ))}
        {responseText ? <p aria-live="polite">{responseText}</p> : null}
        {error ? <p role="alert">{error}</p> : null}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!selected || !draft.trim() || busy) return;
            const next: CompletionRequest["messages"] = [
              ...messages,
              { role: "user", content: draft.trim() },
            ];
            setMessages(next);
            setDraft("");
            setResponseText("");
            setError("");
            setBusy(true);
            let completed = "";
            void streamFailoverCompletion(selected, next, (chunk) => {
              completed += chunk;
              setResponseText(completed);
            })
              .then(() => {
                setMessages([...next, { role: "assistant", content: completed }]);
                setResponseText("");
              })
              .catch(() => setError("Inference is unavailable. Your message was not saved."))
              .finally(() => setBusy(false));
          }}
        >
          <label>
            Message
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              aria-describedby="failover-persistence"
            />
          </label>
          <p id="failover-persistence">This message will not be saved.</p>
          <button type="submit" disabled={!selected || busy}>
            {busy ? "Receiving…" : "Send"}
          </button>
        </form>
      </section>
    </main>
  );
}
