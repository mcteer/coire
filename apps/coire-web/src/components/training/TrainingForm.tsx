import { useEffect, useRef, useState } from "react";
import { submitTraining, trainingYaml, validateTraining, type Dataset, type TrainingSpec, type TrainingSpecDocument, type TrainingSubmission, type TrainingValidation } from "../../api/training";
import { listEvaluationSuites, type EvaluationSuite } from "../../api/evaluations";
import type { components } from "../../api/schema";
import { RegistryBinding } from "./RegistryBinding";
import { MixtureEditor, mixtureError } from "./MixtureEditor";
import { RecipePicker } from "./RecipePicker";

export function initialTrainingSpec(): TrainingSpec {
  return { schema_version: 1, objective: "sft", model: { model_id: "", variant_id: "" },
    data: { train: { datasets: [], epoch_samples: 1, mixture_strategy: "weighted", replacement: false, seed: 0 }, validation: { dataset_ids: [], split: "validation", max_batches: 25, seed: 0 }, loss_policy: "final_assistant" },
    parameterization: { kind: "lora", rank: 8, scale: 20, dropout: 0, target_modules: [], num_layers: 1 },
    optim: { name: "adamw", learning_rate: 0.00001, beta1: 0.9, beta2: 0.999, epsilon: 0.00000001, weight_decay: 0, updates: 100, batch_size: 1, accumulation_steps: 1, max_sequence_length: 2048, schedule: { kind: "constant", warmup_updates: 0 } },
    eval: { loss_every_updates: 100, at_end: true }, output: { adapter_slug: "", checkpoint_every_updates: 100, keep_last_checkpoints: 3 }, placement: { mode: "single", preferred_node: null }, seed: 0 };
}
export function evaluatedTrainingSpec(spec: TrainingSpecDocument, suites: components["schemas"]["TrainingSuiteSchedule"][]): TrainingSpecDocument {
  const { suites: _old, ...loss } = { ...(spec.eval ?? { loss_every_updates: 100, at_end: true }), suites: [] };
  void _old;
  return suites.length ? { ...spec, schema_version: 2, eval: { ...loss, suites } } : { ...spec, schema_version: 1, eval: loss };
}
export function formSubmission(spec: TrainingSpecDocument): TrainingSubmission {
  return { source_kind: "form", source_yaml: trainingYaml(spec), form_spec: spec };
}
export function TrainingForm({ datasets, onSubmitted }: { datasets: Dataset[]; onSubmitted: (id: string) => void }) {
  const [spec, setSpec] = useState<TrainingSpecDocument>(initialTrainingSpec);
  const [suites, setSuites] = useState<EvaluationSuite[]>([]);
  const [suiteError, setSuiteError] = useState("");
  useEffect(() => { let live = true; listEvaluationSuites().then((page) => { if (live) setSuites(page.items.filter((suite) => suite.template.kind !== "harness" && !suite.retired)); }).catch(() => { if (live) setSuiteError("Evaluation suite catalog unavailable. Existing recipes remain usable; refresh to select declared suites."); }); return () => { live = false; }; }, []);
  const declared = spec.schema_version === 2 ? spec.eval.suites : [];
  const schedule = spec.optim.schedule ?? { kind: "constant", warmup_updates: 0 };
  const evaluation = spec.eval ?? { loss_every_updates: 100, at_end: true };
  const placement = spec.placement ?? { mode: "single", preferred_node: null };
  const [mode, setMode] = useState<"form" | "yaml">("form");
  const [yaml, setYaml] = useState("");
  const [targetModulesText, setTargetModulesText] = useState("");
  const [validation, setValidation] = useState<TrainingValidation | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const revision = useRef(0);
  const submitKey = useRef(crypto.randomUUID());
  const changed = () => { revision.current++; setValidation(null); setError(""); submitKey.current = crypto.randomUUID(); };
  const update = (next: TrainingSpecDocument) => { changed(); setSpec(next); };
  const updateLoss = (loss: components["schemas"]["TrainingEvaluation"]) => update(spec.schema_version === 2 ? { ...spec, eval: { ...loss, suites: spec.eval.suites } } : { ...spec, eval: loss });
  const submission = (): TrainingSubmission => mode === "form" ? formSubmission(spec) : { source_kind: "yaml", source_yaml: yaml };
  const validate = async () => {
    setError(""); setBusy(true); const current = revision.current;
    try {
      if (mode === "form") {
        const invalid = mixtureError(spec.data.train);
        if (!spec.model.model_id || !spec.model.variant_id || !spec.output.adapter_slug || !spec.parameterization.target_modules.length) throw new Error("Bind a registry model, variant, datasets, explicit target modules and a unique output slug before validation.");
        if (invalid) throw new Error(invalid);
      }
      const result = await validateTraining(submission()); if (current === revision.current) setValidation(result);
    } catch (e) { if (current === revision.current) setError(String(e)); } finally { setBusy(false); }
  };
  const submit = async () => {
    if (!validation?.ready_to_run) return;
    setBusy(true); setError("");
    try { const receipt = await submitTraining({ ...submission(), preview_sha256: validation.intent_sha256 }, submitKey.current); onSubmitted(receipt.job_id); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const numeric = (label: string, value: number, set: (n: number) => void, min: number, max: number, step = "1") => <label>{label}<input required type="number" min={min} max={max} step={step} value={Number.isFinite(value) ? value : ""} onChange={(e) => set(e.target.valueAsNumber)}/></label>;
  return <section aria-label="New training run"><h1>New supervised training run</h1>
    <RecipePicker onSelect={(recipe) => { changed(); setMode("yaml"); setYaml(recipe.template_yaml); }}/>
    <div className="row"><button className="button ghost" type="button" aria-pressed={mode === "form"} onClick={() => { changed(); setMode("form"); }}>Form</button><button className="button ghost" type="button" aria-pressed={mode === "yaml"} onClick={() => { changed(); setMode("yaml"); setYaml(trainingYaml(spec)); }}>YAML recipe</button></div>
    <form onSubmit={(e) => { e.preventDefault(); void validate(); }} className="training-fields">
      {mode === "yaml" ? <label>Original YAML recipe<textarea rows={24} maxLength={65536} required value={yaml} onChange={(e) => { changed(); setYaml(e.target.value); }}/></label> : <>
        <RegistryBinding modelId={spec.model.model_id} variantId={spec.model.variant_id} onChange={(model_id, variant_id) => update({ ...spec, model: { model_id, variant_id } })}/>
        <MixtureEditor value={spec.data.train} datasets={datasets} onChange={(train) => update({ ...spec, data: { ...spec.data, train, validation: { ...spec.data.validation, dataset_ids: train.datasets.map((d) => d.dataset_id) } } })}/>
        <label>Loss policy<select value={spec.data.loss_policy} onChange={(e) => update({ ...spec, data: { ...spec.data, loss_policy: e.target.value as TrainingSpec["data"]["loss_policy"] } })}><option value="final_assistant">Final assistant (text / tool conversations)</option><option value="all_tokens">All tokens (raw text only)</option></select></label>
        <fieldset className="training-fields"><legend>SFT parameterization · requires backend preflight</legend>
          <label>Parameterization<select value={spec.parameterization.kind} onChange={(e) => update({ ...spec, parameterization: { ...spec.parameterization, kind: e.target.value as TrainingSpec["parameterization"]["kind"] } })}><option value="lora">LoRA (dense base)</option><option value="qlora">QLoRA (acquired affine 4-bit / group-64 base)</option><option value="dora">DoRA (dense base)</option></select></label>
          <label>Explicit target modules<input required value={targetModulesText} onChange={(e) => { setTargetModulesText(e.target.value); update({ ...spec, parameterization: { ...spec.parameterization, target_modules: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } }); }}/></label>
          <p>Only the pinned runtime’s approved linear modules may train. MoE, quantized DoRA, visual models and gradient checkpointing are unavailable.</p>
          {numeric("Adapter rank", spec.parameterization.rank, (rank) => update({ ...spec, parameterization: { ...spec.parameterization, rank } }), 1, 128)}
          {numeric("Adapter scale", spec.parameterization.scale, (scale) => update({ ...spec, parameterization: { ...spec.parameterization, scale } }), 0.000001, 1024, "any")}
          {numeric("Dropout", spec.parameterization.dropout, (dropout) => update({ ...spec, parameterization: { ...spec.parameterization, dropout } }), 0, 0.999999, "any")}
          {numeric("Trainable layers", spec.parameterization.num_layers, (num_layers) => update({ ...spec, parameterization: { ...spec.parameterization, num_layers } }), 1, 256)}
        </fieldset>
        <fieldset className="training-fields"><legend>Optimizer</legend>
          <label>Optimizer<select value={spec.optim.name} onChange={(e) => update({ ...spec, optim: { ...spec.optim, name: e.target.value as TrainingSpec["optim"]["name"], weight_decay: 0 } })}><option value="adamw">AdamW</option><option value="adam">Adam</option></select></label>
          {([['Learning rate', 'learning_rate', 0.000000001, 1, 'any'], ['Beta 1', 'beta1', 0, 0.999999, 'any'], ['Beta 2', 'beta2', 0, 0.999999, 'any'], ['Epsilon', 'epsilon', 0.000000000001, 1, 'any'], ['Weight decay', 'weight_decay', 0, 1, 'any'], ['Completed updates', 'updates', 1, 100000, '1'], ['Global batch size', 'batch_size', 1, 64, '1'], ['Accumulation steps', 'accumulation_steps', 1, 64, '1'], ['Max sequence length', 'max_sequence_length', 2, 8192, '1']] as const).map(([label, key, min, max, step]) => <div key={key}>{numeric(label, spec.optim[key], (n) => update({ ...spec, optim: { ...spec.optim, [key]: n } }), min, max, step)}</div>)}
          <label>Schedule<select value={schedule.kind} onChange={(e) => update({ ...spec, optim: { ...spec.optim, schedule: { kind: e.target.value as NonNullable<TrainingSpec["optim"]["schedule"]>["kind"], warmup_updates: 0 } } })}><option value="constant">Constant</option><option value="warmup_linear">Warmup linear</option></select></label>
          {schedule.kind === "warmup_linear" && numeric("Warmup updates", schedule.warmup_updates, (warmup_updates) => update({ ...spec, optim: { ...spec.optim, schedule: { ...schedule, warmup_updates } } }), 0, spec.optim.updates)}
        </fieldset>
        <fieldset className="training-fields"><legend>Held-out evaluation and output</legend>
          {numeric("Held-out loss every updates", evaluation.loss_every_updates, (loss_every_updates) => updateLoss({ ...evaluation, loss_every_updates }), 1, 100000)}
          {numeric("Validation max batches", spec.data.validation.max_batches, (max_batches) => update({ ...spec, data: { ...spec.data, validation: { ...spec.data.validation, max_batches } } }), 1, 1000)}
          {numeric("Validation seed", spec.data.validation.seed, (seed) => update({ ...spec, data: { ...spec.data, validation: { ...spec.data.validation, seed } } }), 0, 4294967295)}
          <label><input type="checkbox" checked={evaluation.at_end} onChange={(e) => updateLoss({ ...evaluation, at_end: e.target.checked })}/>Evaluate held-out loss at end</label>
          <label>Unique adapter slug<input required pattern="[a-z0-9][a-z0-9-]*" maxLength={80} value={spec.output.adapter_slug} onChange={(e) => update({ ...spec, output: { ...spec.output, adapter_slug: e.target.value } })}/></label>
          {numeric("Checkpoint every updates", spec.output.checkpoint_every_updates, (checkpoint_every_updates) => update({ ...spec, output: { ...spec.output, checkpoint_every_updates } }), 1, 100000)}
          {numeric("Retained complete checkpoints", spec.output.keep_last_checkpoints, (keep_last_checkpoints) => update({ ...spec, output: { ...spec.output, keep_last_checkpoints } }), 1, 3)}
        </fieldset>
        <fieldset><legend>Optional task and judge evaluations</legend><p>Only declared suites run automatically at completion. Select up to four registered suite versions. Checkpoints must be unique, increasing, before the final update and aligned with checkpoint cadence.</p>
          {suiteError && <p role="status">{suiteError}</p>}
          {suites.map((suite) => { const selected = declared.find((item) => item.suite_id === suite.suite_id && item.suite_version === suite.version); return <div key={`${suite.suite_id}:${suite.version}`}><label><input type="checkbox" checked={!!selected} disabled={!selected && declared.length >= 4} onChange={(event) => update(evaluatedTrainingSpec(spec, event.target.checked ? [...declared, { suite_id: suite.suite_id, suite_version: suite.version, checkpoint_updates: [] }] : declared.filter((item) => item !== selected)))}/>{suite.suite_id} version {suite.version} · {suite.template.kind} / {suite.template.mode}</label>
            {selected && <label>Checkpoint updates for {suite.suite_id} version {suite.version}<input value={selected.checkpoint_updates?.join(",") ?? ""} pattern="[0-9, ]*" onChange={(event) => update(evaluatedTrainingSpec(spec, declared.map((item) => item === selected ? { ...item, checkpoint_updates: event.target.value.trim() ? event.target.value.split(",").map((value) => Number(value.trim())) : [] } : item)))}/></label>}</div>; })}
        </fieldset>
        <label>Placement<select value={placement.mode} onChange={(e) => update({ ...spec, placement: { mode: e.target.value as NonNullable<TrainingSpec["placement"]>["mode"], preferred_node: null } })}><option value="single">Single Studio · preflight required</option><option value="data_parallel" disabled>Two Studios · capability unavailable</option></select></label>
        <label>Preferred Studio<select value={placement.preferred_node ?? ""} onChange={(e) => update({ ...spec, placement: { ...placement, preferred_node: (e.target.value || null) as NonNullable<TrainingSpec["placement"]>["preferred_node"] } })}><option value="">Automatic placement</option><option value="coire-edge-a">coire-edge-a</option><option value="coire-edge-b">coire-edge-b</option></select></label>
        {numeric("Training seed", spec.seed, (seed) => update({ ...spec, seed }), 0, 4294967295)}
        <details open><summary>Generated source YAML (JSON-compatible YAML)</summary><pre className="mono">{trainingYaml(spec)}</pre></details>
      </>}
      <p>Chat has priority. Unmeasured combinations cannot start; temporary contention queues, impossible fit refuses. SFT supports held-out loss and explicitly declared task/judge suites.</p>
      {error && <p role="alert" className="error">{error}</p>}
      <button className="button ghost" disabled={busy}>{busy ? "Working…" : "Validate and resolve"}</button>
      {validation && <section aria-label="Resolved preview"><h3>{validation.ready_to_run ? "Ready after measured preflight" : "Preflight pending / capability unavailable"}</h3>
        <ul>{(validation.reasons ?? []).map((reason) => <li key={reason}>{reason.replaceAll("_", " ")}</li>)}</ul>
        <p className="mono">Intent digest: {validation.intent_sha256}</p>
        {validation.resolved ? <pre className="mono">{JSON.stringify(validation.resolved, null, 2)}</pre> : <p>Resolved runtime, immutable inputs and measured resource envelope are unavailable.</p>}
      </section>}
      <button className="button" type="button" disabled={busy || !validation?.ready_to_run} onClick={() => void submit()}>Submit training run</button>
    </form>
  </section>;
}
