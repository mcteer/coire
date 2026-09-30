import { useState, type FormEvent } from "react";
import type { ChatTurn, RegisteredWorkspace } from "../../api/chat";

export function CodeControls({
  action,
  onAction,
  workspaces,
  workspaceId,
  onWorkspace,
  sourceRevision,
  onRevision,
  researchTurns,
  researchId,
  onResearch,
  planTurns,
  planId,
  onPlan,
  onRegister,
  disabled,
}: {
  action: "research" | "plan" | "apply";
  onAction: (value: "research" | "plan" | "apply") => void;
  workspaces: RegisteredWorkspace[];
  workspaceId: string | null;
  onWorkspace: (id: string | null) => void;
  sourceRevision: string;
  onRevision: (value: string) => void;
  researchTurns: ChatTurn[];
  researchId: string | null;
  onResearch: (id: string | null) => void;
  planTurns: ChatTurn[];
  planId: string | null;
  onPlan: (id: string | null) => void;
  onRegister: (url: string) => Promise<boolean>;
  disabled: boolean;
}) {
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [registering, setRegistering] = useState(false);
  const register = async (event: FormEvent) => {
    event.preventDefault();
    if (!repositoryUrl.trim() || registering) return;
    setRegistering(true);
    try {
      if (await onRegister(repositoryUrl)) setRepositoryUrl("");
    } finally {
      setRegistering(false);
    }
  };
  return (
    <section className="chat-code-controls glass" aria-label="Code mode">
      <div className="chat-code-actions" role="group" aria-label="Code action">
        {(["research", "plan", "apply"] as const).map((value) => (
          <button
            className="button"
            type="button"
            key={value}
            aria-pressed={action === value}
            disabled={disabled}
            onClick={() => onAction(value)}
          >
            {value[0].toUpperCase() + value.slice(1)}
          </button>
        ))}
      </div>
      <label htmlFor="chat-code-workspace">Registered repository</label>
      <select
        id="chat-code-workspace"
        value={workspaceId ?? ""}
        disabled={disabled || workspaces.length === 0}
        onChange={(event) => onWorkspace(event.target.value || null)}
      >
        {workspaces.length === 0 && <option value="">No registered repositories</option>}
        {workspaces.map((workspace) => (
          <option value={workspace.id} key={workspace.id}>
            {workspace.repository_url}
          </option>
        ))}
      </select>
      <form className="chat-code-register" onSubmit={(event) => void register(event)}>
        <label htmlFor="chat-code-register-url">Add HTTPS repository</label>
        <input
          id="chat-code-register-url"
          type="url"
          value={repositoryUrl}
          placeholder="https://github.com/org/repo.git"
          onChange={(event) => setRepositoryUrl(event.target.value)}
          disabled={disabled || registering}
        />
        <button
          className="button"
          type="submit"
          disabled={disabled || registering || !repositoryUrl.trim()}
        >
          Add repository
        </button>
      </form>
      <label htmlFor="chat-code-revision">Source revision</label>
      <input
        id="chat-code-revision"
        value={sourceRevision}
        maxLength={128}
        disabled={disabled || action === "apply" || (action === "plan" && Boolean(researchId))}
        onChange={(event) => onRevision(event.target.value)}
      />
      {action === "plan" && (
        <>
          <label htmlFor="chat-code-research">Prior research (optional)</label>
          <select
            id="chat-code-research"
            value={researchId ?? ""}
            disabled={disabled}
            onChange={(event) => onResearch(event.target.value || null)}
          >
            <option value="">No prior research</option>
            {researchTurns.map((turn) => (
              <option key={turn.id} value={turn.coding_call_id ?? ""}>
                Research from {new Date(turn.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </>
      )}
      {action === "plan" && researchId && (
        <p className="muted">
          The plan uses the selected research result's recorded source revision.
        </p>
      )}
      {action === "apply" && (
        <>
          <label htmlFor="chat-code-plan">Plan to apply</label>
          <select
            id="chat-code-plan"
            value={planId ?? ""}
            disabled={disabled || planTurns.length === 0}
            onChange={(event) => onPlan(event.target.value || null)}
          >
            <option value="">
              {planTurns.length === 0 ? "Create a plan first" : "Choose a plan"}
            </option>
            {planTurns.map((turn) => (
              <option key={turn.id} value={turn.coding_call_id ?? ""}>
                Plan from {new Date(turn.created_at).toLocaleString()}
              </option>
            ))}
          </select>
          <p className="muted">
            Apply starts from a fresh workspace snapshot at the selected plan revision. The model
            must be verified for write tasks.
          </p>
        </>
      )}
    </section>
  );
}
