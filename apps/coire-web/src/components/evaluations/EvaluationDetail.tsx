import { useCallback, useEffect, useRef, useState } from "react";
import { cancelEvaluation, rerunEvaluation, decodeEvaluationEvent, evaluationEventsUrl, getEvaluation, getEvaluationEvidence, type EvaluationEvent, type EvaluationRun } from "../../api/evaluations";
import { useEventStream } from "../../hooks/useEventStream";
import { Preferences } from "./Preferences";
const terminal = (state: string) => ["succeeded", "failed", "timed_out", "cancelled"].includes(state);
export function EvaluationDetail({ id }: { id: string }) {
  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [error, setError] = useState("");
  const [accepted, setAccepted] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const operation = useRef<{ name: "cancel" | "rerun"; version: number; key: string } | null>(null);
  const [evidence, setEvidence] = useState<string | null>(null);
  const selectedId = useRef(id);
  selectedId.current = id;
  const refresh = useCallback(async () => { try { const value = await getEvaluation(id); if (selectedId.current !== id || value.id !== id) return; setRun(value); setError(""); } catch (cause) { if (selectedId.current === id) { setRun(null); setEvidence(null); setError(String(cause)); } } }, [id]);
  useEffect(() => { setRun(null); setEvidence(null); setAccepted(null); setBusy(false); operation.current = null; void refresh(); }, [refresh]);
  const stream = useEventStream<EvaluationEvent>(run && !terminal(run.state) ? evaluationEventsUrl(id) : "", null, { decode: (frame) => decodeEvaluationEvent(frame, id), isTerminal: (event) => event.kind === "terminal" });
  useEffect(() => { if (stream.data?.snapshot?.id === id) setRun(stream.data.snapshot); else if (stream.data) void refresh(); }, [stream.data, refresh, id]);
  useEffect(() => { if (!run || terminal(run.state)) return; const timer = window.setInterval(() => void refresh(), 5000); return () => window.clearInterval(timer); }, [run, refresh]);
  const control = async (name: "cancel" | "rerun") => {
    if (!run || busy) return;
    if (operation.current?.name !== name || operation.current.version !== run.version) operation.current = { name, version: run.version, key: crypto.randomUUID() };
    const current = operation.current;
    setBusy(true); setError("");
    try {
      const receipt = await (name === "cancel" ? cancelEvaluation : rerunEvaluation)(id, current.version, current.key);
      if (selectedId.current !== id) return;
      operation.current = null;
      if (name === "rerun") setAccepted(receipt.id);
      await refresh();
    } catch (cause) { if (selectedId.current === id) setError(String(cause)); } finally { if (selectedId.current === id) setBusy(false); }
  };
  return <section aria-label="Evaluation detail" className="training-card"><h3>Evaluation {id}</h3>
    {error && <p role="alert">{error}</p>}{accepted && <p role="status">New evaluation accepted: {accepted}. Open it in evaluation history.</p>}{!run && !error && <p role="status">Loading evaluation…</p>}
    {run && <><p role="status">{run.state} · {run.suite.suite_id} version {run.suite.version} · {run.suite.template.kind} / {run.suite.template.mode}</p>
      {run.reason && <p>{run.reason.replaceAll("_", " ")}</p>}
      {!terminal(run.state) && <p>{stream.connected ? "Live evaluation connected" : "Refreshing persisted evaluation while reconnecting."}</p>}
      {stream.error && <p>{stream.error}</p>}
      {terminal(run.state) ? <button disabled={busy || run.suite.retired} onClick={() => void control("rerun")}>Rerun with fresh evaluation ID</button> : <button disabled={busy || run.state === "cancelling"} onClick={() => void control("cancel")}>Cancel evaluation</button>}
      {run.suite.retired && <p>Suite retired; historical results remain readable, new runs are refused.</p>}
      <p>Training success and exact-target harness verification are tracked separately from task and judge scores.</p>
      {run.result ? <><table aria-label="Subject evaluation scores"><thead><tr><th>Exact subject</th><th>{run.suite.template.mode === "pairwise" ? "Preference credit" : "Score"}</th></tr></thead><tbody>{run.result.subjects.map((subject, index) => <tr key={index}><td>{subject.public_selector}</td><td>{run.result?.outcome === "succeeded" ? run.result.aggregates[index]?.toFixed(3) ?? "Unavailable" : "Unavailable"}</td></tr>)}</tbody></table>
        {run.suite.template.mode === "pairwise" && <p>Independent quality scores are not measured by this pairwise suite.</p>}
        <p>Training input overlap: {run.result.contamination.status.replaceAll("_", " ")} · {run.result.contamination.hit_count} cases · {run.result.contamination.checked_cases} checked.</p>
        {run.result.contamination.reason && <p>{run.result.contamination.reason.replaceAll("_", " ")}</p>}
        {!!run.result.pairwise?.length && <Preferences cases={run.result.pairwise}/>}
        <details><summary>Immutable score and provenance</summary><pre>{JSON.stringify(run.result, null, 2)}</pre></details></> : <p>Scores pending; no aggregate is available.</p>}
      <details><summary>Frozen suite, decoding and exact runtime identities</summary><pre>{JSON.stringify({ suite: run.suite, subjects: run.subjects }, null, 2)}</pre></details>
      {(run.evidence ?? []).map((item) => <div key={item.id}>{item.availability === "present" ? <button onClick={() => void getEvaluationEvidence(id, item.id).then((value) => { if (selectedId.current === id) setEvidence(JSON.stringify(value, null, 2)); }).catch((cause) => { if (selectedId.current === id) setError(String(cause)); })}>Read private evidence {item.id}</button> : <p>Evidence {item.availability}; scores and provenance remain available.</p>}</div>)}
      {evidence && <details open><summary>Private evidence</summary><pre>{evidence}</pre><button onClick={() => setEvidence(null)}>Close private evidence</button></details>}
    </>}
  </section>;
}
