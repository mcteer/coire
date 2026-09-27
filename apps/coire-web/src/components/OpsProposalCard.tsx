import { useState } from "react";
import {
  confirmOpsProposal,
  declineOpsProposal,
  type OpsProposal,
  type OpsProposalIssued,
} from "../api/client";

export function OpsProposalCard({ issued }: { issued: OpsProposalIssued }) {
  const [proposal, setProposal] = useState<OpsProposal>(issued.proposal);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const pending = proposal.state === "pending";
  const decide = async (decision: "approve" | "decline") => {
    setWorking(true);
    setError("");
    try {
      setProposal(
        decision === "approve"
          ? await confirmOpsProposal({ ...issued, proposal })
          : await declineOpsProposal(proposal.id, "Declined in Ask Coire"),
      );
    } catch (reason) {
      setError(String(reason));
    } finally {
      setWorking(false);
    }
  };
  return (
    <article className="panel ops-proposal" aria-label="Action proposal">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h4>Confirmation required</h4>
        <span className="status">{proposal.state}</span>
      </div>
      <p>{proposal.rationale}</p>
      <dl className="proposal-details">
        <dt>Operation</dt>
        <dd className="mono">{proposal.action.operation}</dd>
        <dt>Target</dt>
        <dd className="mono">
          {proposal.action.target_type}:{proposal.action.target_id}
        </dd>
        <dt>Expected state</dt>
        <dd>{proposal.action.precondition.expected_state}</dd>
        <dt>Parameters</dt>
        <dd className="mono">{JSON.stringify(proposal.action.parameters)}</dd>
        <dt>Expires</dt>
        <dd>{new Date(proposal.expires_at).toLocaleString()}</dd>
      </dl>
      {pending && (
        <div className="actions">
          <button className="button" disabled={working} onClick={() => void decide("approve")}>
            {working ? "Working…" : "Approve exact action"}
          </button>
          <button
            className="button secondary"
            disabled={working}
            onClick={() => void decide("decline")}
          >
            Decline
          </button>
        </div>
      )}
      {proposal.result && <p className="mono">{JSON.stringify(proposal.result)}</p>}
      {error && <p className="error">{error}</p>}
    </article>
  );
}
