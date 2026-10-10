import { useRef, useState, type FormEvent } from "react";
import {
  submitFeedbackExport,
  type PreferenceExportCreate,
  type PreferenceExportReceipt,
} from "../../api/feedback";
import { RegistryBinding } from "../training/RegistryBinding";

export function ExportForm({
  onSubmitted,
}: {
  onSubmitted: (receipt: PreferenceExportReceipt) => void;
}) {
  const [modelId, setModelId] = useState("");
  const [variantId, setVariantId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [accepted, setAccepted] = useState<string | null>(null);
  const saving = useRef(false);
  const attempt = useRef<{ input: string; key: string } | null>(null);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving.current || !modelId || !variantId) return;
    const form = new FormData(event.currentTarget);
    const text = (name: string) => String(form.get(name) ?? "").trim();
    const source = text("source");
    if (source !== "owner_preferred" && source !== "owner" && source !== "admin") return;
    const filters: PreferenceExportCreate["filters"] = {};
    for (const key of ["model_id", "variant_id", "adapter_id", "owner_id", "tag"] as const) {
      if (text(key)) filters[key] = text(key);
    }
    if ((filters.variant_id || filters.adapter_id) && !filters.model_id) {
      setError("A generating variant or adapter filter needs its parent model.");
      return;
    }
    if (text("from")) filters.from = new Date(text("from")).toISOString();
    if (text("until")) filters.until = new Date(text("until")).toISOString();
    if (filters.from && filters.until && filters.from >= filters.until) {
      setError("The end of the date range must follow its beginning.");
      return;
    }
    const body: PreferenceExportCreate = {
      name: text("name"),
      license_note: text("license"),
      model_id: modelId,
      variant_id: variantId,
      source,
      filters,
      split_seed: Number(form.get("seed")),
      validation_fraction: Number(form.get("fraction")),
    };
    const input = JSON.stringify(body);
    if (attempt.current?.input !== input) attempt.current = { input, key: crypto.randomUUID() };
    saving.current = true;
    setBusy(true);
    setError("");
    setAccepted(null);
    try {
      const receipt = await submitFeedbackExport(body, attempt.current.key);
      setAccepted(receipt.id);
      attempt.current = null;
      onSubmitted(receipt);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Export submission failed.");
    } finally {
      saving.current = false;
      setBusy(false);
    }
  };
  return (
    <section aria-label="Export explicit feedback">
      <h2>Export explicit pairs</h2>
      <p>Exports use eligible explicit comparisons. Thumbs do not become training pairs.</p>
      <p>
        Fewer than 20 pairs warns about a small sample. At least two distinct prompts are needed for
        separate training and validation sets.
      </p>
      <p>
        Published datasets and trained adapters remain after capture is disabled or a conversation
        is deleted.
      </p>
      <form className="training-fields" onSubmit={(event) => void submit(event)}>
        <label>
          Dataset name
          <input name="name" required maxLength={120} disabled={busy} />
        </label>
        <label>
          License and consent note
          <input name="license" required maxLength={2048} disabled={busy} />
        </label>
        <RegistryBinding
          modelId={modelId}
          variantId={variantId}
          onChange={(model, variant) => {
            if (!saving.current) {
              setModelId(model);
              setVariantId(variant);
            }
          }}
        />
        <label>
          Judgement source
          <select name="source" defaultValue="owner_preferred" disabled={busy}>
            <option value="owner_preferred">Owner choice, otherwise admin</option>
            <option value="owner">Owner choices only</option>
            <option value="admin">Admin choices only</option>
          </select>
        </label>
        <label>
          Judgement tag
          <input name="tag" maxLength={32} pattern="[a-z0-9][a-z0-9-]*" disabled={busy} />
        </label>
        <details>
          <summary>Optional source filters</summary>
          {(
            [
              ["model_id", "Generating model ID"],
              ["variant_id", "Generating variant ID"],
              ["adapter_id", "Generating adapter ID"],
              ["owner_id", "Owner ID"],
            ] as const
          ).map(([name, label]) => (
            <label key={name}>
              {label}
              <input
                name={name}
                maxLength={36}
                pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                disabled={busy}
              />
            </label>
          ))}
          <label>
            Judgement from
            <input name="from" type="datetime-local" disabled={busy} />
          </label>
          <label>
            Judgement until
            <input name="until" type="datetime-local" disabled={busy} />
          </label>
        </details>
        <label>
          Split seed
          <input
            name="seed"
            type="number"
            min={0}
            max={4294967295}
            defaultValue={0}
            required
            disabled={busy}
          />
        </label>
        <label>
          Validation fraction
          <input
            name="fraction"
            type="number"
            min={0.000001}
            max={0.999999}
            step="any"
            defaultValue={0.05}
            required
            disabled={busy}
          />
        </label>
        <button className="button" type="submit" disabled={busy || !modelId || !variantId}>
          {busy ? "Submitting export…" : "Export explicit pairs"}
        </button>
      </form>
      {error && (
        <p role="alert">{error} Retrying unchanged input uses the same request identity.</p>
      )}
      {accepted && (
        <p role="status">
          Export queued: <span className="mono">{accepted}</span>. Follow its progress below.
        </p>
      )}
    </section>
  );
}
