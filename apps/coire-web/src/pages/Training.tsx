import { useCallback, useEffect, useState } from "react";
import { listTrainingJobs, type Dataset, type TrainingJob } from "../api/training";
import { TrainingForm } from "../components/training/TrainingForm";
import { TrainingRun } from "../components/training/TrainingRun";
import { DatasetPanel } from "../components/training/DatasetPanel";
import { AdapterPanel } from "../components/training/AdapterPanel";
import { TrainingMeasurements } from "../components/training/TrainingMeasurements";
import { EvaluationForm } from "../components/evaluations/EvaluationForm";
import { EvaluationHistory } from "../components/evaluations/EvaluationHistory";
import { ExportForm } from "../components/feedback/ExportForm";
import { ReviewQueue } from "../components/feedback/ReviewQueue";
import { ExportHistory } from "../components/feedback/ExportHistory";
import "../styles/training.css";

export function Training({ isAdmin }: { isAdmin: boolean }) {
  if (!isAdmin)
    return (
      <main className="training-page">
        <section className="panel glass">
          <h1>Admin access required</h1>
          <p>Your current role cannot manage datasets, training or adapters.</p>
        </section>
      </main>
    );
  return <TrainingWorkspace />;
}
function TrainingWorkspace() {
  const [evaluations, setEvaluations] = useState(() => location.hash === "#training/evaluations"),
    [evaluationRevision, setEvaluationRevision] = useState(0);
  useEffect(() => {
    const changed = () => setEvaluations(location.hash === "#training/evaluations");
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const [jobs, setJobs] = useState<TrainingJob[] | null>(null),
    [cursor, setCursor] = useState<string | null>(null),
    [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(() =>
      location.hash.startsWith("#training/run/")
        ? location.hash.slice("#training/run/".length)
        : null,
    ),
    [datasets, setDatasets] = useState<Dataset[]>([]);
  const [panel, setPanel] = useState<"datasets" | "adapters" | "capabilities" | "feedback">(
    "datasets",
  );
  const [exportRevision, setExportRevision] = useState(0),
    [datasetRevision, setDatasetRevision] = useState(0);
  const reload = useCallback(async () => {
    const page = await listTrainingJobs();
    setJobs(page.items);
    setCursor(page.next_cursor ?? null);
    setError("");
  }, []);
  useEffect(() => {
    let live = true;
    const refresh = () => {
      if (live)
        void reload().catch((e) => {
          if (live) setError(String(e));
        });
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [reload]);
  return (
    <main className="training-page">
      <aside className="training-rail glass" aria-label="Training runs">
        <h2>Runs</h2>
        {error && (
          <p role="alert" className="error">
            Training unavailable or disabled: {error}
          </p>
        )}
        {jobs === null && !error && <p role="status">Loading runs…</p>}
        {jobs?.length === 0 && <p>No training runs yet.</p>}
        {jobs?.map((job) => (
          <button
            className={`training-run-link ${selected === job.id ? "selected" : ""}`}
            key={job.id}
            aria-pressed={selected === job.id}
            onClick={() => {
              setSelected(job.id);
              setEvaluations(false);
            }}
          >
            <strong>{job.spec.output.adapter_slug}</strong>
            <span className="status">{job.state}</span>
            <small>
              SFT · {job.spec.parameterization.kind} · update {job.completed_update}
              {job.reason ? ` · ${job.reason.replaceAll("_", " ")}` : ""}
            </small>
          </button>
        ))}
        {cursor && (
          <button
            className="button ghost"
            onClick={() =>
              void listTrainingJobs(cursor)
                .then((page) => {
                  setJobs((old) => [...(old ?? []), ...page.items]);
                  setCursor(page.next_cursor ?? null);
                })
                .catch((e) => setError(String(e)))
            }
          >
            Load older runs
          </button>
        )}
        <button
          className="button training-new"
          onClick={() => {
            setSelected(null);
            setEvaluations(false);
          }}
        >
          New run from recipe
        </button>
        <button
          onClick={() => {
            setEvaluations(true);
            location.hash = "#training/evaluations";
          }}
          aria-pressed={evaluations}
        >
          Evaluation runs
        </button>
        <a href="#admin/activity">Admin Runs & jobs / kill visibility</a>
      </aside>
      <div className="training-main glass">
        {evaluations ? (
          <>
            <EvaluationForm onSubmitted={() => setEvaluationRevision((value) => value + 1)} />
            <EvaluationHistory refreshKey={evaluationRevision} />
          </>
        ) : selected ? (
          <TrainingRun key={selected} id={selected} />
        ) : (
          <TrainingForm
            datasets={datasets}
            onSubmitted={(id) => {
              setSelected(id);
              void reload().catch((e) => setError(String(e)));
            }}
          />
        )}
      </div>
      <aside className="training-side glass">
        <div className="row" role="group" aria-label="Training resources">
          {(["datasets", "adapters", "capabilities", "feedback"] as const).map((name) => (
            <button
              type="button"
              key={name}
              className="button ghost"
              aria-pressed={panel === name}
              onClick={() => setPanel(name)}
            >
              {name[0].toUpperCase() + name.slice(1)}
            </button>
          ))}
        </div>
        <div hidden={panel !== "datasets"}>
          <DatasetPanel onChange={setDatasets} refreshKey={datasetRevision} />
        </div>
        {panel === "feedback" && (
          <>
            <ReviewQueue />
            <ExportForm onSubmitted={() => setExportRevision((value) => value + 1)} />
            <ExportHistory
              refreshKey={exportRevision}
              onDataset={() => {
                setPanel("datasets");
                setDatasetRevision((value) => value + 1);
              }}
            />
          </>
        )}
        {panel === "adapters" && <AdapterPanel />}
        {panel === "capabilities" && <TrainingMeasurements />}
      </aside>
    </main>
  );
}
