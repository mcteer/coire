import { type FormEvent, useState } from "react";
import {
  sendOpsMessage,
  startOpsConversation,
  type OpsConversation,
  type OpsTurnResponse,
} from "../../api/client";
import { OpsProposalCard } from "../../components/OpsProposalCard";

export function AskCoire() {
  const [conversation, setConversation] = useState<OpsConversation | null>(null);
  const [question, setQuestion] = useState("");
  const [turn, setTurn] = useState<OpsTurnResponse | null>(null);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState("");
  const ask = async (event: FormEvent) => {
    event.preventDefault();
    setAsking(true);
    setError("");
    try {
      const active = conversation ?? (await startOpsConversation());
      setConversation(active);
      setTurn(await sendOpsMessage(active.id, question));
    } catch (reason) {
      setError(String(reason));
    } finally {
      setAsking(false);
    }
  };
  return (
    <section className="panel glass">
      <h3>Ask Coire</h3>
      <p className="muted">Grounded operations help. Every mutation requires exact approval.</p>
      {turn?.degraded && (
        <p className="error banner" role="status">
          Read-only degraded mode — action approvals are unavailable.
        </p>
      )}
      <form onSubmit={ask}>
        <div className="field">
          <label htmlFor="ops-question">Question</label>
          <textarea
            id="ops-question"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            required
          />
        </div>
        <button className="button" disabled={asking}>
          {asking ? "Checking…" : "Ask"}
        </button>
      </form>
      {turn && (
        <div className={turn.degraded ? "error ask-answer" : "ask-answer"}>
          {turn.answer}
          <p className="muted mono">Sources: {(turn.sources ?? []).join(", ")}</p>
        </div>
      )}
      {turn?.proposal && !turn.degraded && <OpsProposalCard issued={turn.proposal} />}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
