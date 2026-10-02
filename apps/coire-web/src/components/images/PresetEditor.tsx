import { useState } from "react";
import {
  createImagePreset,
  retireImagePreset,
  updateImagePreset,
  type ImageModelList,
  type ImagePresetList,
} from "../../api/images";
import { ConfirmAction } from "../ConfirmAction";

type Preset = ImagePresetList["items"][number];

export function PresetEditor({
  presets,
  models = [],
  canEdit,
  onChanged = async () => {},
}: {
  presets: ImagePresetList["items"];
  models?: ImageModelList["items"];
  canEdit: boolean;
  onChanged?: () => Promise<void>;
}) {
  const [source, setSource] = useState<Preset | null>(null);
  const [name, setName] = useState("");
  const [prefix, setPrefix] = useState("");
  const [prompt, setPrompt] = useState("");
  const [modelId, setModelId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const edit = (preset: Preset) => {
    setSource(preset);
    setName(preset.name);
    setPrefix(preset.prompt_prefix);
    setPrompt(preset.defaults.prompt ?? "");
    setModelId(preset.defaults.model_id ?? "");
    setError("");
  };

  const create = () => {
    setSource(null);
    setName("");
    setPrefix("");
    setPrompt("");
    setModelId(models[0]?.id ?? "");
    setError("");
  };

  const save = async () => {
    if (!name.trim() || !modelId || !prompt.trim()) return;
    setBusy(true);
    setError("");
    try {
      const defaults = source
        ? {
            ...source.defaults,
            schema_version: 1 as const,
            model_id: modelId,
            preset_id: null,
            preset_revision: null,
            prompt: prompt.trim(),
          }
        : { schema_version: 1 as const, model_id: modelId, prompt: prompt.trim() };
      if (source) {
        await updateImagePreset(source.id, {
          expected_revision: source.revision,
          name: name.trim(),
          prompt_prefix: prefix,
          defaults,
        });
      } else {
        await createImagePreset({ name: name.trim(), prompt_prefix: prefix, defaults });
      }
      await onChanged();
      create();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };

  const retire = async (preset: Preset) => {
    setBusy(true);
    setError("");
    try {
      await retireImagePreset(preset.id);
      await onChanged();
      if (source?.id === preset.id) create();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel glass wide" aria-label="Image presets">
      <h2>Presets</h2>
      {presets.length === 0 ? (
        <p>No presets.</p>
      ) : (
        <ul>
          {presets.map((preset) => (
            <li key={`${preset.id}:${preset.revision}`}>
              {preset.name}
              <span className="mono"> · revision {preset.revision}</span>
              {preset.retired ? " · retired" : ""}
              {canEdit && !preset.retired && (
                <>
                  <button
                    className="button"
                    type="button"
                    disabled={busy}
                    onClick={() => edit(preset)}
                  >
                    Edit {preset.name}
                  </button>
                  <ConfirmAction
                    target={preset.name}
                    label="Retire"
                    ariaLabel={`Retire ${preset.name}`}
                    onConfirm={() => retire(preset)}
                  />
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {canEdit && (
        <form
          className="image-preset-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <h3>{source ? `Edit ${source.name}` : "New preset"}</h3>
          <label>
            Name
            <input
              aria-label="Preset name"
              maxLength={120}
              required
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            Prompt prefix
            <input
              aria-label="Prompt prefix"
              maxLength={1000}
              value={prefix}
              onChange={(event) => setPrefix(event.target.value)}
            />
          </label>
          <label>
            Model
            <select
              aria-label="Preset model"
              required
              value={modelId}
              onChange={(event) => setModelId(event.target.value)}
            >
              <option value="">Choose a model</option>
              {source?.defaults.model_id &&
                !models.some((model) => model.id === source.defaults.model_id) && (
                  <option value={source.defaults.model_id}>{source.defaults.model_id}</option>
                )}
              {models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.display_name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Default prompt
            <textarea
              aria-label="Default prompt"
              required
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
            />
          </label>
          <button className="button" type="submit" disabled={busy || !modelId}>
            {busy ? "Saving…" : "Save preset"}
          </button>
          {source && (
            <button className="button" type="button" onClick={create}>
              New preset
            </button>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </form>
      )}
    </section>
  );
}
