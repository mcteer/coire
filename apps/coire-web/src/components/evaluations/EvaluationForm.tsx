import { useEffect, useRef, useState } from "react";
import { listEvaluationSuites, submitEvaluation, type EvaluationSuite, type EvaluationSubmission } from "../../api/evaluations";
import { listAdapters, type Adapter } from "../../api/training";
import { RegistryBinding } from "../training/RegistryBinding";
export function EvaluationForm({ onSubmitted }: { onSubmitted: (id: string) => void }) {
  const [suites, setSuites] = useState<EvaluationSuite[]>([]), [adapters, setAdapters] = useState<Adapter[]>([]);
  const [suiteKey, setSuiteKey] = useState(""), [model, setModel] = useState(""), [variant, setVariant] = useState(""), [adapter, setAdapter] = useState(""), [againstBase, setAgainstBase] = useState(false);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), [available, setAvailable] = useState(false);
  const key = useRef(crypto.randomUUID());
  const changed = () => { key.current = crypto.randomUUID(); setError(""); };
  useEffect(() => { let live = true; Promise.all([listEvaluationSuites(), listAdapters()]).then(([catalog, registered]) => { if (!live) return; setSuites(catalog.items.filter((suite) => !suite.retired)); setAdapters(registered.items.filter((row) => row.state === "ready")); setAvailable(true); }).catch((cause) => { if (live) setError(String(cause)); }); return () => { live = false; }; }, []);
  const suite = suites.find((item) => `${item.suite_id}:${item.version}` === suiteKey);
  const selectedAdapter = adapters.find((item) => item.id === adapter);
  const selfJudge = suite?.judge && (suite.judge.target.model_id === model || (selectedAdapter && suite.judge.target.base_manifest_sha256 === selectedAdapter.base_manifest_sha256));
  const invalid = !suite || !model || !variant || !!selfJudge || (suite.template.mode === "pairwise" && (!adapter || !againstBase));
  const submit = async () => { if (busy || invalid || !suite) return; setBusy(true); setError("");
    const subject = { model_id: model, variant_id: variant, adapter_id: adapter || null };
    const body: EvaluationSubmission = { suite_id: suite.suite_id, suite_version: suite.version, subjects: againstBase && adapter ? [{ ...subject, adapter_id: null }, subject] : [subject] };
    try { const receipt = await submitEvaluation(body, key.current); onSubmitted(receipt.id); key.current = crypto.randomUUID(); } catch (cause) { setError(String(cause)); } finally { setBusy(false); }
  };
  return <section aria-label="New evaluation"><h2>Run an evaluation</h2><p>Fixed suites run on Studios with read-only model access. Task/judge scores do not grant write access.</p>
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <label>Registered evaluation suite<select required value={suiteKey} disabled={busy || !available} onChange={(event) => { changed(); setSuiteKey(event.target.value); }}><option value="">Select a suite version</option>{suites.map((item) => <option key={`${item.suite_id}:${item.version}`} value={`${item.suite_id}:${item.version}`}>{item.suite_id} version {item.version} · {item.template.kind} / {item.template.mode}</option>)}</select></label>
      <fieldset disabled={busy}><legend>Exact model and variant</legend><RegistryBinding modelId={model} variantId={variant} onChange={(modelId, variantId) => { changed(); setModel(modelId); setVariant(variantId); setAdapter(""); setAgainstBase(false); }}/></fieldset>
      <label>Exact evaluation adapter<select value={adapter} disabled={busy || !variant} onChange={(event) => { changed(); setAdapter(event.target.value); setAgainstBase(false); }}><option value="">Base only</option>{adapters.filter((row) => row.model_id === model && row.base_variant_id === variant).map((row) => <option key={row.id} value={row.id}>{row.slug} · {row.verified ? "verified" : "unverified"} · {row.visibility}</option>)}</select></label>
      {suite?.template.kind !== "harness" && <label><input type="checkbox" disabled={busy || !adapter} checked={againstBase} onChange={(event) => { changed(); setAgainstBase(event.target.checked); }}/>Compare against exact base</label>}
      {suite?.judge && <p>Fixed judge: {suite.judge.public_selector}. Runtime: {suite.judge.runtime.engine_version}. Judge selection is frozen in this suite version.</p>}
      {selfJudge && <p role="alert">A judge cannot evaluate its own model, variant, alias or adapter.</p>}
      {error && <p role="alert">{error}</p>}
      <button disabled={busy || !available || invalid}>{busy ? "Submitting evaluation…" : "Submit evaluation"}</button>
    </form>
  </section>;
}
