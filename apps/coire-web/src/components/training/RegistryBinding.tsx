import { useEffect, useState } from "react";
import { listTrainingModels, listTrainingVariants, type RegistryModel, type RegistryVariant } from "../../api/training";

/** Only existing, acquired ready Studio language-model revisions are offered. */
export function RegistryBinding({ modelId, variantId, onChange }: {
  modelId: string; variantId: string; onChange: (modelId: string, variantId: string) => void;
}) {
  const [models, setModels] = useState<RegistryModel[]>([]);
  const [variants, setVariants] = useState<RegistryVariant[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let live = true;
    listTrainingModels().then((rows) => { if (live) setModels(rows.filter((m) => m.state === "ready" && m.source === "studio" && m.kind === "language_model" && m.backend === "mlx_lm")); })
      .catch((e) => { if (live) setError(String(e)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    let live = true;
    setVariants([]);
    if (modelId) listTrainingVariants(modelId).then((rows) => { if (live) setVariants(rows.filter((v) => v.state === "ready")); })
      .catch((e) => { if (live) setError(String(e)); });
    return () => { live = false; };
  }, [modelId]);
  return <fieldset className="training-fields"><legend>Registry model and immutable variant</legend>
    {error && <p role="alert" className="error">{error}</p>}
    {loading && <p role="status">Loading registry…</p>}
    <label>Base model<select required value={modelId} onChange={(e) => onChange(e.target.value, "")}>
      <option value="">Select an acquired ready model</option>{models.map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}
    </select></label>
    <label>Base variant<select required value={variantId} disabled={!modelId} onChange={(e) => onChange(modelId, e.target.value)}>
      <option value="">Select a ready variant</option>{variants.map((v) => <option key={v.id} value={v.id}>{v.name} · {v.precision}</option>)}
    </select></label>
    {!loading && !models.length && <p>No ready local language models are available.</p>}
  </fieldset>;
}
