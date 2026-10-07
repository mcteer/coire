import { useCallback, useEffect, useState, type FormEvent } from "react";
import { analyzeDataset, deleteDataset, getDatasetAnalysis, listDatasets, uploadDataset, type Dataset, type DatasetAnalysis, type DatasetUpload } from "../../api/training";
import { ConfirmAction } from "../ConfirmAction";
import { RegistryBinding } from "./RegistryBinding";

export function DatasetPanel({ onChange }: { onChange?: (datasets: Dataset[]) => void }) {
  const [rows, setRows] = useState<Dataset[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [analyses, setAnalyses] = useState<Record<string, DatasetAnalysis>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [modelId, setModelId] = useState("");
  const [variantId, setVariantId] = useState("");
  const [format, setFormat] = useState<DatasetUpload["format"]>("conversation");
  const reload = useCallback(async () => {
    const page = await listDatasets();
    setRows(page.items); setCursor(page.next_cursor ?? null); onChange?.(page.items);
  }, [onChange]);
  useEffect(() => { void reload().catch((e) => setError(String(e))); }, [reload]);
  useEffect(() => {
    let live = true;
    const refresh = async () => {
      for (const row of rows ?? []) if (row.analysis_id) {
        try { const analysis = await getDatasetAnalysis(row.analysis_id); if (live) setAnalyses((old) => ({ ...old, [row.id]: analysis })); }
        catch (e) { if (live) setError(String(e)); }
      }
      if (live && rows?.some((r) => ["analyzing", "validating", "uploading"].includes(r.state))) await reload().catch((e) => { if (live) setError(String(e)); });
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => { live = false; window.clearInterval(timer); };
  }, [rows, reload]);
  const action = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); await reload(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const upload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget), file = form.get("file");
    if (!(file instanceof File)) return;
    const metadata: DatasetUpload = {
      name: String(form.get("name")), format,
      provenance: { source: String(form.get("source")), license_note: String(form.get("license")) },
      analysis_model_id: modelId, analysis_variant_id: variantId,
      split_seed: Number(form.get("seed")), validation_fraction: Number(form.get("fraction")),
    };
    await action(() => uploadDataset(file, metadata, crypto.randomUUID()));
  };
  return <section aria-label="Datasets" className="training-datasets">
    <div className="row"><h2>Datasets</h2><button className="button ghost" onClick={() => setShowUpload(!showUpload)} aria-expanded={showUpload}>Upload JSONL</button></div>
    <p className="muted">Private uploads · 256 MiB maximum · text and tools only. <a href="/docs/runbooks/sft-training">Dataset help</a></p>
    {error && <p role="alert" className="error">{error}</p>}
    {!showUpload && Boolean(rows?.length) && <RegistryBinding modelId={modelId} variantId={variantId} onChange={(model, variant) => { setModelId(model); setVariantId(variant); }}/>}
    {showUpload && <form onSubmit={(e) => void upload(e)} className="training-card training-fields">
      <label>Name<input name="name" required maxLength={120}/></label>
      <label>Format<select value={format} onChange={(e) => setFormat(e.target.value as DatasetUpload["format"])}><option value="text">Raw text</option><option value="prompt_completion">Prompt / completion</option><option value="conversation">Text / tool conversation</option></select></label>
      <label>JSONL file<input type="file" name="file" accept=".jsonl" required/></label>
      <label>Provenance source<input name="source" required maxLength={2048}/></label>
      <label>License note<input name="license" required maxLength={2048}/></label>
      <RegistryBinding modelId={modelId} variantId={variantId} onChange={(model, variant) => { setModelId(model); setVariantId(variant); }}/>
      <label>Split seed<input name="seed" type="number" min={0} max={4294967295} defaultValue={0} required/></label>
      <label>Validation fraction<input name="fraction" type="number" min={0.000001} max={0.999999} step="any" defaultValue={0.05} required/></label>
      <button className="button" disabled={busy || !modelId || !variantId}>{busy ? "Uploading…" : "Upload and analyze"}</button>
    </form>}
    {rows === null && !error && <p role="status">Loading datasets…</p>}
    {rows?.length === 0 && <p>No datasets yet. Upload JSONL to register an immutable source.</p>}
    {rows?.map((dataset) => {
      const analysis = analyses[dataset.id];
      return <article className="training-card" key={dataset.id} aria-label={dataset.name}>
        <h3>{dataset.name} <span className="status">{dataset.state.replaceAll("_", " ")}</span></h3>
        <p className="mono">{dataset.format} · {dataset.row_count} rows · revision {dataset.version}</p>
        <details><summary>Immutable provenance and retention</summary><dl>
          <dt>Dataset ID</dt><dd className="mono">{dataset.id}</dd><dt>Source digest</dt><dd className="mono">{dataset.source_sha256 ?? "Unavailable"}</dd>
          <dt>Split digest / seed</dt><dd className="mono">{dataset.split_manifest_sha256 ?? "Pending"} / {dataset.split_seed}</dd>
          <dt>Source</dt><dd>{dataset.provenance.source}</dd><dt>License</dt><dd>{dataset.provenance.license_note}</dd>
        </dl><p>Active and paused jobs pin their inputs. Purging preserves terminal provenance and removes reproducibility.</p></details>
        {analysis && <div>
          <p>Analysis: {analysis.state} · <span className="mono">{analysis.id}</span></p>
          {analysis.state === "succeeded" && analysis.tokens ? <>
            <p className="mono">Tokens p50 {analysis.tokens.p50} · p95 {analysis.tokens.p95} · max {analysis.tokens.maximum} · duplicates {analysis.duplicate_rows}</p>
            <ul aria-label="Token histogram">{analysis.tokens.upper_bounds.map((bound, i) => <li key={bound}>≤ {bound} tokens: {analysis.tokens?.histogram[i]} rows</li>)}</ul>
            <p>Roles: {Object.entries(analysis.role_counts ?? {}).map(([role, count]) => `${role} ${count}`).join(" · ")}</p>
            <details><summary>Tokenizer / template identity</summary><p className="mono">{analysis.model_id} / {analysis.variant_id}<br/>{analysis.tokenizer_sha256}<br/>{analysis.template_sha256}</p></details>
          </> : <p>Statistics unavailable until successful Studio analysis.</p>}
        </div>}
        {(dataset.invalid_count > 0 || (analysis?.invalid_count ?? 0) > 0) && <p className="error">Invalid rows: {Math.max(dataset.invalid_count, analysis?.invalid_count ?? 0)}. This source is not eligible for training.</p>}
        <ul>{[...(dataset.diagnostics ?? []), ...(analysis?.diagnostics ?? [])].slice(0, 100).map((d, i) => <li key={i}>Row {d.row} · {d.field}: {d.code.replaceAll("_", " ")}</li>)}</ul>
        {!['failed', 'purged', 'retired'].includes(dataset.state) && <>
          <button className="button ghost" disabled={busy || !modelId || !variantId} onClick={() => void action(() => analyzeDataset(dataset.id, { model_id: modelId, variant_id: variantId }, crypto.randomUUID()))}>Reanalyze / retry with selected variant</button>
        </>}
        {dataset.state !== "purged" && <ConfirmAction target={dataset.name} label="Delete" onConfirm={() => action(() => deleteDataset(dataset, crypto.randomUUID()))}/>}
      </article>;
    })}
    {cursor && <button className="button ghost" disabled={busy} onClick={() => void listDatasets(cursor).then((page) => { setRows((old) => [...(old ?? []), ...page.items]); setCursor(page.next_cursor ?? null); }).catch((e) => setError(String(e)))}>Load older datasets</button>}
  </section>;
}
