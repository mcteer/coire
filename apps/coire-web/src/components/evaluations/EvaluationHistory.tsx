import { useCallback, useEffect, useRef, useState } from "react";
import { listEvaluations, type EvaluationHistoryFilters, type EvaluationRun } from "../../api/evaluations";
import { EvaluationDetail } from "./EvaluationDetail";
export function EvaluationHistory({ refreshKey = 0 }: { refreshKey?: number }) {
  const [rows, setRows] = useState<EvaluationRun[] | null>(null), [cursor, setCursor] = useState<string | null>(null), [error, setError] = useState("");
  const [filters, setFilters] = useState<EvaluationHistoryFilters>({}), [selected, setSelected] = useState<string | null>(null);
  const request = useRef(0);
  const refresh = useCallback(async () => { const version = ++request.current; try { const page = await listEvaluations(filters); if (version !== request.current) return; setRows(page.items); setCursor(page.next_cursor ?? null); setError(""); } catch (cause) { if (version !== request.current) return; setRows(null); setCursor(null); setSelected(null); setError(String(cause)); } }, [filters]);
  useEffect(() => { void refresh(); }, [refresh, refreshKey]);
  return <section aria-label="Evaluation history"><h2>Evaluation history</h2>
    <label>Evaluation outcome<select value={filters.state ?? ""} onChange={(event) => setFilters({ ...filters, state: event.target.value ? event.target.value as EvaluationHistoryFilters["state"] : undefined })}><option value="">All outcomes</option>{["queued", "preparing", "reserving", "running", "collecting", "cancelling", "succeeded", "failed", "timed_out", "cancelled"].map((state) => <option key={state} value={state}>{state.replaceAll("_", " ")}</option>)}</select></label>
    <label>Training job ID<input value={filters.training_job_id ?? ""} onChange={(event) => setFilters({ ...filters, training_job_id: event.target.value || undefined })}/></label>
    <label>Adapter ID<input value={filters.adapter_id ?? ""} onChange={(event) => setFilters({ ...filters, adapter_id: event.target.value || undefined })}/></label>
    <button onClick={() => void refresh()}>Refresh evaluation history</button>
    {error && <p role="alert">{error}</p>}{rows === null && !error && <p role="status">Loading evaluation history…</p>}{rows?.length === 0 && <p>No evaluation runs match these filters.</p>}
    {rows?.map((run) => <article key={run.id}><button aria-pressed={selected === run.id} onClick={() => setSelected(selected === run.id ? null : run.id)}>{run.suite.suite_id} version {run.suite.version} · {run.state} · {run.created_at}</button><p>{run.subjects.map((subject) => subject.public_selector).join(" versus ")}{run.suite.retired ? " · retired suite; history retained" : ""}</p>{selected === run.id && <EvaluationDetail id={run.id}/>}</article>)}
    {cursor && <button onClick={() => { const version = ++request.current; void listEvaluations({ ...filters, cursor }).then((page) => { if (version !== request.current) return; setRows((old) => [...(old ?? []), ...page.items.filter((item) => !(old ?? []).some((previous) => previous.id === item.id))]); setCursor(page.next_cursor ?? null); }).catch((cause) => { if (version !== request.current) return; setRows(null); setCursor(null); setSelected(null); setError(String(cause)); }); }}>Load older evaluations</button>}
  </section>;
}
