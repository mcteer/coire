import { useEffect, useState } from "react";
import {
  chatArtifactUrl,
  getBranchArtifactMetadata,
  type BranchArtifact,
  type ChatRunActivity,
  type ChatRunActivityStatus,
  type ChatTurnResult,
} from "../../api/chat";

export function RunActivity({
  conversationId,
  turnId,
  activity,
  status,
  result,
}: {
  conversationId: string;
  turnId: string;
  activity: ChatRunActivity["activity"][];
  status?: ChatRunActivityStatus;
  result?: ChatTurnResult;
}) {
  const [visible, setVisible] = useState(50);
  const artifactId =
    result?.tool === "apply" && "artifact_id" in result.result ? result.result.artifact_id : null;
  const [artifact, setArtifact] = useState<BranchArtifact | null>(null);
  const [artifactUnavailable, setArtifactUnavailable] = useState(false);
  useEffect(() => {
    if (!artifactId) return;
    let live = true;
    void getBranchArtifactMetadata(artifactId)
      .then((value) => {
        if (live) setArtifact(value);
      })
      .catch(() => {
        if (live) setArtifactUnavailable(true);
      });
    return () => {
      live = false;
    };
  }, [artifactId]);
  const shown = activity.slice(-visible);
  return (
    <section className="chat-run-activity glass" aria-label="Coding run activity">
      <h2>Code run</h2>
      {activity.length === 0 && <p className="muted">Waiting for tool activity…</p>}
      {activity.length > visible && (
        <button className="button" type="button" onClick={() => setVisible((count) => count + 50)}>
          Show earlier activity ({activity.length - visible} hidden)
        </button>
      )}
      <ol className="chat-run-steps" start={activity.length - shown.length + 1}>
        {shown.map((record) => (
          <li key={`${record.run_id}:${record.sequence}`}>
            <strong>{record.tool_name.replaceAll("_", " ")}</strong> · {record.state}
            {record.duration_ms != null && <span> · {record.duration_ms} ms</span>}
            {record.safe_error && <span> · {record.safe_error.replaceAll("_", " ")}</span>}
          </li>
        ))}
      </ol>
      {status && (
        <p role="status">
          Activity {status.state}
          {status.state !== "complete" ? ` at record ${status.last_sequence}` : ""}
        </p>
      )}
      {result?.tool === "research" && "answer" in result.result && (
        <div>
          <h3>Research</h3>
          <p>{result.result.answer}</p>
          <ul>
            {result.result.citations.map((citation) => (
              <li key={`${citation.path}:${citation.line}`}>
                {citation.path}:{citation.line}
              </li>
            ))}
          </ul>
        </div>
      )}
      {result?.tool === "plan" && "steps" in result.result && (
        <div>
          <h3>Plan</h3>
          <ol>
            {result.result.steps.map((step, index) => (
              <li key={index}>
                {step.description}
                <ul>
                  {step.acceptance_criteria.map((criterion) => (
                    <li key={criterion}>{criterion}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        </div>
      )}
      {result?.tool === "apply" && "branch" in result.result && (
        <div>
          <h3>Apply result</h3>
          <p>
            Branch {result.result.branch} · Tests {result.result.tests.status}
          </p>
          {(result.result.tests.command ?? []).length > 0 && (
            <p>Command: {(result.result.tests.command ?? []).join(" ")}</p>
          )}
          <details>
            <summary>Diff excerpt{result.result.diff_truncated ? " (truncated)" : ""}</summary>
            <pre>{result.result.diff_excerpt}</pre>
          </details>
          {artifact && new Date(artifact.expires_at).getTime() > Date.now() ? (
            <>
              <p>Bundle expires {new Date(artifact.expires_at).toLocaleString()}</p>
              <a className="button" href={chatArtifactUrl(conversationId, turnId)} download>
                Download branch bundle
              </a>
            </>
          ) : artifactUnavailable ||
            (artifact && new Date(artifact.expires_at).getTime() <= Date.now()) ? (
            <p role="status">Branch bundle expired or unavailable.</p>
          ) : (
            <p>Checking branch bundle…</p>
          )}
        </div>
      )}
    </section>
  );
}
