import type { Dataset, TrainingSpec } from "../../api/training";
type Mixture = TrainingSpec["data"]["train"];
export function mixtureError(mixture: Mixture): string | null {
  if (!mixture.datasets.length || mixture.datasets.length > 16) return "Select 1–16 immutable dataset sources.";
  if (new Set(mixture.datasets.map((d) => d.dataset_id)).size !== mixture.datasets.length) return "Each dataset may appear only once.";
  if (mixture.datasets.some((d) => !Number.isInteger(d.sample_count) || d.sample_count < 1 || d.sample_count > 1000000 || !Number.isFinite(d.mixture_proportion) || d.mixture_proportion <= 0 || d.mixture_proportion > 1)) return "Each source needs a positive sample count and proportion.";
  if (Math.abs(mixture.datasets.reduce((sum, d) => sum + d.mixture_proportion, 0) - 1) > 1e-6) return "Mixture proportions must sum to one.";
  if (!Number.isInteger(mixture.epoch_samples) || mixture.epoch_samples < 1) return "Epoch samples must be a positive integer.";
  if (!mixture.replacement && mixture.epoch_samples > mixture.datasets.reduce((sum, d) => sum + d.sample_count, 0)) return "Epoch samples exceed source pools without replacement.";
  return null;
}
export function MixtureEditor({ value, datasets, onChange }: { value: Mixture; datasets: Dataset[]; onChange: (value: Mixture) => void }) {
  const update = (index: number, changes: Partial<Mixture["datasets"][number]>) => onChange({ ...value, datasets: value.datasets.map((d, i) => i === index ? { ...d, ...changes } : d) });
  const available = datasets.filter((d) => d.state === "ready");
  const error = mixtureError(value);
  return <fieldset className="training-fields"><legend>Deterministic training mixture</legend>
    <p>Immutable source IDs and split digests are frozen at preflight. Sample pools and cross-source duplicate leakage are validated on the Studio; no merged corpus is created.</p>
    {value.datasets.map((source, i) => <div className="training-card training-fields" key={i}>
      <label>Dataset source {i + 1}<select required value={source.dataset_id} onChange={(e) => update(i, { dataset_id: e.target.value })}><option value="">Select a ready dataset</option>{available.map((d) => <option key={d.id} value={d.id}>{d.name} · {d.source_sha256?.slice(0, 12)}</option>)}</select></label>
      <label>Source {i + 1} sample pool<input type="number" required min={1} max={1000000} value={source.sample_count} onChange={(e) => update(i, { sample_count: e.target.valueAsNumber })}/></label>
      <label>Source {i + 1} proportion<input type="number" required min={0.000001} max={1} step="any" value={source.mixture_proportion} onChange={(e) => update(i, { mixture_proportion: e.target.valueAsNumber })}/></label>
      <button className="button ghost" type="button" onClick={() => onChange({ ...value, datasets: value.datasets.filter((_, n) => n !== i) })}>Remove source {i + 1}</button>
    </div>)}
    <button type="button" className="button ghost" disabled={value.datasets.length >= 16 || !available.length} onClick={() => onChange({ ...value, datasets: [...value.datasets, { dataset_id: "", split: "train", sample_count: 1, mixture_proportion: 1 }] })}>Add dataset source</button>
    <label>Epoch samples<input required type="number" min={1} max={16000000} value={value.epoch_samples} onChange={(e) => onChange({ ...value, epoch_samples: e.target.valueAsNumber })}/></label>
    <label>Mixture strategy<select value={value.mixture_strategy} onChange={(e) => onChange({ ...value, mixture_strategy: e.target.value as Mixture["mixture_strategy"] })}><option value="weighted">Weighted quotas</option><option value="sequential">Sequential quotas</option></select></label>
    <label><input type="checkbox" checked={value.replacement} onChange={(e) => onChange({ ...value, replacement: e.target.checked })}/>Sample with replacement</label>
    <label>Mixture seed<input type="number" min={0} max={4294967295} required value={value.seed} onChange={(e) => onChange({ ...value, seed: e.target.valueAsNumber })}/></label>
    {error && <p role="status" className="error">{error}</p>}
  </fieldset>;
}
