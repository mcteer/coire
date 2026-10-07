import { useCallback, useEffect, useState } from "react";
import { listTrainingJobs, type Dataset, type TrainingJob } from "../api/training";
import { TrainingForm } from "../components/training/TrainingForm";
import { TrainingRun } from "../components/training/TrainingRun";
import { DatasetPanel } from "../components/training/DatasetPanel";
import { AdapterPanel } from "../components/training/AdapterPanel";
import { TrainingMeasurements } from "../components/training/TrainingMeasurements";
import "../styles/training.css";

export function Training({ isAdmin }: { isAdmin: boolean }) {
  if (!isAdmin) return <main className="training-page"><section className="panel glass"><h1>Admin access required</h1><p>Your current role cannot manage datasets, training or adapters.</p></section></main>;
  return <TrainingWorkspace/>;
}
function TrainingWorkspace() {
  const [jobs, setJobs] = useState<TrainingJob[] | null>(null), [cursor, setCursor] = useState<string | null>(null), [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(() => location.hash.startsWith("#training/run/") ? location.hash.slice("#training/run/".length) : null), [datasets, setDatasets] = useState<Dataset[]>([]);
  const [panel, setPanel] = useState<"datasets" | "adapters" | "capabilities">("datasets");
  const reload = useCallback(async () => { const page = await listTrainingJobs(); setJobs(page.items); setCursor(page.next_cursor ?? null); setError(""); }, []);
  useEffect(() => { let live = true; const refresh = () => { if (live) void reload().catch((e) => { if (live) setError(String(e)); }); }; refresh(); const timer = window.setInterval(refresh, 5000); return () => { live = false; window.clearInterval(timer); }; }, [reload]);
  return <main className="training-page">
    <aside className="training-rail glass" aria-label="Training runs">
      <h2>Runs</h2>{error && <p role="alert" className="error">Training unavailable or disabled: {error}</p>}
      {jobs === null && !error && <p role="status">Loading runs…</p>}{jobs?.length === 0 && <p>No training runs yet.</p>}
      {jobs?.map((job) => <button className={`training-run-link ${selected === job.id ? "selected" : ""}`} key={job.id} aria-pressed={selected === job.id} onClick={() => setSelected(job.id)}><strong>{job.spec.output.adapter_slug}</strong><span className="status">{job.state}</span><small>SFT · {job.spec.parameterization.kind} · update {job.completed_update}{job.reason ? ` · ${job.reason.replaceAll("_", " ")}` : ""}</small></button>)}
      {cursor && <button className="button ghost" onClick={() => void listTrainingJobs(cursor).then((page) => { setJobs((old) => [...(old ?? []), ...page.items]); setCursor(page.next_cursor ?? null); }).catch((e) => setError(String(e)))}>Load older runs</button>}
      <button className="button training-new" onClick={() => setSelected(null)}>New run from recipe</button>
      <a href="#admin/activity">Admin Runs & jobs / kill visibility</a>
    </aside>
    <div className="training-main glass">{selected ? <TrainingRun key={selected} id={selected}/> : <TrainingForm datasets={datasets} onSubmitted={(id) => { setSelected(id); void reload().catch((e) => setError(String(e))); }}/>}</div>
    <aside className="training-side glass"><div className="row" role="group" aria-label="Training resources">{(["datasets", "adapters", "capabilities"] as const).map((name) => <button type="button" key={name} className="button ghost" aria-pressed={panel === name} onClick={() => setPanel(name)}>{name[0].toUpperCase() + name.slice(1)}</button>)}</div>
      <div hidden={panel !== "datasets"}><DatasetPanel onChange={setDatasets}/></div>
      {panel === "adapters" && <AdapterPanel/>}{panel === "capabilities" && <TrainingMeasurements/>}
    </aside>
  </main>;
}
