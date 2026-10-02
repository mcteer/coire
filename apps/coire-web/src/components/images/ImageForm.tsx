import { useEffect, useRef, useState } from "react";
import type { ImageModelList, ImagePresetList, ImageSubmitRequest } from "../../api/images";
import { ImageInputField } from "./ImageInputField";
import { PresetRail } from "./PresetRail";

type Model = ImageModelList["items"][number];

function capabilityOf(model: Model | undefined): Model["capability"] | null {
  if (!model || !("capability" in model) || model.capability == null) return null;
  return model.capability;
}

function storageSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toLocaleString()} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toLocaleString()} MiB`;
  return `${bytes.toLocaleString()} bytes`;
}

export function ImageForm({
  models,
  presets,
  disabled,
  onSubmit,
  reuse,
  limits = null,
}: {
  models: ImageModelList["items"];
  presets: ImagePresetList["items"];
  disabled: boolean;
  onSubmit: (request: ImageSubmitRequest) => Promise<void>;
  reuse?: { request: ImageSubmitRequest; revision: number } | null;
  limits?: ImageModelList["limits"];
}) {
  const [modelId, setModelId] = useState(models[0]?.id ?? "");
  const [presetId, setPresetId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [width, setWidth] = useState<number | null>(null);
  const [height, setHeight] = useState<number | null>(null);
  const [steps, setSteps] = useState<number | null>(null);
  const [seed, setSeed] = useState<number | null>(null);
  const [count, setCount] = useState<number | null>(null);
  const [mode, setMode] = useState<NonNullable<ImageSubmitRequest["mode"]>>("txt2img");
  const [negativePrompt, setNegativePrompt] = useState("");
  const [guidance, setGuidance] = useState<string | null>(null);
  const [initImageId, setInitImageId] = useState<string | null>(null);
  const [maskId, setMaskId] = useState<string | null>(null);
  const [controlImageId, setControlImageId] = useState<string | null>(null);
  const [controlModelId, setControlModelId] = useState<string | null>(null);
  const [controlStrength, setControlStrength] = useState("1");
  const [lowThreshold, setLowThreshold] = useState(100);
  const [highThreshold, setHighThreshold] = useState(200);
  const [strength, setStrength] = useState<string | null>(null);
  const [loras, setLoras] = useState<NonNullable<ImageSubmitRequest["loras"]>>([]);
  const [upscale, setUpscale] = useState<NonNullable<ImageSubmitRequest["upscale"]> | null>(null);
  const [reusedRequest, setReusedRequest] = useState<ImageSubmitRequest | null>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (!reusedRequest && !modelId && models[0]) setModelId(models[0].id);
  }, [modelId, models, reusedRequest]);
  useEffect(() => {
    if (!reuse) return;
    const request = reuse.request;
    setReusedRequest(request);
    setPresetId(null);
    setModelId(request.model_id ?? "");
    setPrompt(request.prompt ?? "");
    setWidth(request.width ?? null);
    setHeight(request.height ?? null);
    setSteps(request.steps ?? null);
    setSeed(request.seed ?? null);
    setCount(request.n ?? null);
    setMode(request.mode ?? "txt2img");
    setNegativePrompt(request.negative_prompt ?? "");
    setGuidance(request.guidance == null ? null : String(request.guidance));
    setInitImageId(request.init_image_id ?? null);
    setMaskId(request.mask_id ?? null);
    setControlImageId(request.control?.image_id ?? null);
    setControlModelId(request.control?.model_id ?? null);
    setControlStrength(request.control?.strength == null ? "1" : String(request.control.strength));
    setLowThreshold(request.control?.low_threshold ?? 100);
    setHighThreshold(request.control?.high_threshold ?? 200);
    setStrength(request.strength == null ? null : String(request.strength));
    setLoras(request.loras ?? []);
    setUpscale(request.upscale ?? null);
    promptRef.current?.focus();
  }, [reuse]);
  const preset = presets.find((item) => item.id === presetId) ?? null;
  const model =
    models.find((item) => item.id === modelId) ?? (reusedRequest ? undefined : models[0]);
  const capability = preset ? null : capabilityOf(model);
  const missingModel = preset === null && !models.some((item) => item.id === modelId);
  const unsupportedMode = Boolean(capability && !capability.modes.includes(mode));
  const unsupportedControlLoras = mode === "control" && loras.length > 0;
  const unavailableControl = Boolean(
    controlModelId && !model?.controls?.some((available) => available.id === controlModelId),
  );
  const invalidControlSettings =
    mode === "control" &&
    (!Number.isFinite(Number(controlStrength)) ||
      Number(controlStrength) <= 0 ||
      Number(controlStrength) > 1 ||
      !Number.isInteger(lowThreshold) ||
      !Number.isInteger(highThreshold) ||
      lowThreshold < 0 ||
      highThreshold > 255 ||
      lowThreshold >= highThreshold);
  const unavailableLora = loras.some(
    (item) => !model?.loras?.some((available) => available.id === item.model_id),
  );
  const tooManyLoras = Boolean(capability && loras.length > capability.max_loras);
  const invalidLoraScale = loras.some(
    (item) => !Number.isFinite(Number(item.scale)) || Number(item.scale) < -2 || Number(item.scale) > 2,
  );
  const unavailableUpscale = Boolean(
    upscale && !model?.upscalers?.some((available) => available.id === upscale.model_id),
  );
  const sourceWidth = width ?? capability?.default_width ?? null;
  const sourceHeight = height ?? capability?.default_height ?? null;
  const upscaleExceedsBounds = Boolean(
    upscale &&
      sourceWidth &&
      sourceHeight &&
      (sourceWidth * upscale.factor > 4096 ||
        sourceHeight * upscale.factor > 4096 ||
        sourceWidth * sourceHeight * upscale.factor ** 2 > 16_000_000),
  );
  const missingInput =
    preset === null &&
    (((mode === "img2img" || mode === "fill") && !initImageId) ||
      (mode === "img2img" && !strength) ||
      (mode === "fill" && !maskId) ||
      (mode === "control" && (!controlImageId || !controlModelId)));

  const cannotSubmit =
    disabled ||
    !limits ||
    prompt.trim() === "" ||
    missingModel ||
    unsupportedMode ||
    unsupportedControlLoras ||
    unavailableControl ||
    invalidControlSettings ||
    missingInput ||
    unavailableLora ||
    unavailableUpscale ||
    upscaleExceedsBounds ||
    tooManyLoras ||
    invalidLoraScale;

  const submit = async () => {
    const text = prompt.trim();
    if (cannotSubmit) return;
    const control: ImageSubmitRequest["control"] =
      mode === "control" && controlImageId && controlModelId
        ? {
            type: "canny",
            image_id: controlImageId,
            model_id: controlModelId,
            strength: controlStrength,
            low_threshold: lowThreshold,
            high_threshold: highThreshold,
          }
        : null;
    const overrides: Pick<ImageSubmitRequest, "width" | "height" | "steps" | "seed" | "n"> = {};
    if (width != null) overrides.width = width;
    if (height != null) overrides.height = height;
    if (steps != null) overrides.steps = steps;
    if (seed != null) overrides.seed = seed;
    if (count != null) overrides.n = count;
    if (preset) {
      await onSubmit({
        schema_version: 1,
        preset_id: preset.id,
        preset_revision: preset.revision,
        prompt: text,
        ...overrides,
      });
      return;
    }
    if (!modelId) return;
    if (reusedRequest) {
      await onSubmit({
        ...reusedRequest,
        model_id: modelId,
        mode,
        prompt: text,
        negative_prompt: negativePrompt || null,
        guidance,
        width,
        height,
        steps,
        seed,
        n: count,
        loras,
        upscale,
        init_image_id: mode === "img2img" || mode === "fill" ? initImageId : null,
        mask_id: mode === "fill" ? maskId : null,
        strength: mode === "img2img" ? strength : null,
        control,
      });
      return;
    }
    await onSubmit({
      schema_version: 1,
      model_id: modelId,
      mode,
      prompt: text,
      negative_prompt: negativePrompt || null,
      guidance,
      init_image_id: mode === "img2img" || mode === "fill" ? initImageId : null,
      mask_id: mode === "fill" ? maskId : null,
      strength: mode === "img2img" ? strength : null,
      control,
      loras,
      upscale,
      ...overrides,
    });
  };

  return (
    <form
      className="image-generation-form"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <PresetRail
        presets={presets}
        selectedId={presetId}
        onSelect={(selected) => {
          setPresetId(selected);
          if (reusedRequest) setPrompt("");
          setReusedRequest(null);
          if (selected) setMode("txt2img");
        }}
      />
      {preset === null && (
        <label>
          Model
          <select
            aria-label="Image model"
            value={modelId}
            onChange={(event) => {
              setModelId(event.target.value);
              setLoras([]);
              setUpscale(null);
              setControlModelId(null);
              setControlImageId(null);
            }}
          >
            {models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.display_name}
              </option>
            ))}
          </select>
        </label>
      )}
      {capability && (
        <div className="image-settings-grid">
          <label>
            Mode
            <select
              aria-label="Image mode"
              value={mode}
              onChange={(event) =>
                setMode(event.target.value as NonNullable<ImageSubmitRequest["mode"]>)
              }
            >
              {capability.modes.map((available) => (
                <option key={available} value={available}>
                  {available}
                </option>
              ))}
            </select>
          </label>
          <label>
            Width
            <input
              aria-label="Image width"
              type="number"
              min={capability.min_width}
              max={capability.max_width}
              step={8}
              placeholder={String(capability.default_width ?? capability.min_width)}
              value={width ?? ""}
              onChange={(event) =>
                setWidth(event.target.value === "" ? null : Number(event.target.value))
              }
            />
          </label>
          <label>
            Height
            <input
              aria-label="Image height"
              type="number"
              min={capability.min_height}
              max={capability.max_height}
              step={8}
              placeholder={String(capability.default_height ?? capability.min_height)}
              value={height ?? ""}
              onChange={(event) =>
                setHeight(event.target.value === "" ? null : Number(event.target.value))
              }
            />
          </label>
          <label>
            Steps
            <input
              aria-label="Image steps"
              type="number"
              min={capability.min_steps}
              max={capability.max_steps}
              placeholder={String(capability.default_steps ?? capability.min_steps)}
              value={steps ?? ""}
              onChange={(event) =>
                setSteps(event.target.value === "" ? null : Number(event.target.value))
              }
            />
          </label>
          <label>
            Images
            <input
              aria-label="Image count"
              type="number"
              min={1}
              max={capability.max_outputs}
              placeholder="1"
              value={count ?? ""}
              onChange={(event) =>
                setCount(event.target.value === "" ? null : Number(event.target.value))
              }
            />
          </label>
          <label>
            Seed
            <input
              aria-label="Image seed"
              type="number"
              min={0}
              placeholder="random"
              value={seed ?? ""}
              onChange={(event) =>
                setSeed(event.target.value === "" ? null : Number(event.target.value))
              }
            />
          </label>
          <label>
            Guidance
            <input
              aria-label="Image guidance"
              type="number"
              min={capability.min_guidance}
              max={capability.max_guidance}
              step="any"
              value={guidance ?? ""}
              placeholder={capability.default_guidance ?? "0"}
              onChange={(event) => setGuidance(event.target.value || null)}
            />
          </label>
          {capability.supports_negative_prompt && (
            <label>
              Negative prompt
              <textarea
                aria-label="Image negative prompt"
                value={negativePrompt}
                maxLength={4000}
                onChange={(event) => setNegativePrompt(event.target.value)}
              />
            </label>
          )}
          {capability.max_loras > 0 && (
            <fieldset>
              <legend>LoRA adapters</legend>
              {loras.map((selected, index) => (
                <div key={selected.model_id}>
                  <span>
                    {model?.loras?.find((item) => item.id === selected.model_id)?.display_name ??
                      "Unavailable adapter"}
                  </span>
                  <label>
                    Scale {index + 1}
                    <input
                      aria-label={`LoRA scale ${index + 1}`}
                      type="number"
                      min={-2}
                      max={2}
                      step="any"
                      value={selected.scale}
                      onChange={(event) =>
                        setLoras((current) =>
                          current.map((item, position) =>
                            position === index ? { ...item, scale: event.target.value } : item,
                          ),
                        )
                      }
                    />
                  </label>
                  <button
                    type="button"
                    aria-label={`Remove LoRA ${index + 1}`}
                    onClick={() => setLoras((current) => current.filter((_, position) => position !== index))}
                  >
                    Remove
                  </button>
                </div>
              ))}
              <label>
                Add LoRA
                <select
                  aria-label="Add LoRA"
                  value=""
                  disabled={loras.length >= capability.max_loras}
                  onChange={(event) => {
                    const id = event.target.value;
                    if (id) setLoras((current) => [...current, { model_id: id, scale: "1" }]);
                  }}
                >
                  <option value="">Choose adapter</option>
                  {(model?.loras ?? [])
                    .filter((available) => !loras.some((item) => item.model_id === available.id))
                    .map((available) => (
                      <option key={available.id} value={available.id}>
                        {available.display_name}
                      </option>
                    ))}
                </select>
              </label>
            </fieldset>
          )}
          {(model?.upscalers?.length ?? 0) > 0 && (
            <fieldset>
              <legend>Upscale</legend>
              <label>
                Upscale model
                <select
                  aria-label="Upscale model"
                  value={upscale?.model_id ?? ""}
                  onChange={(event) =>
                    setUpscale(
                      event.target.value
                        ? { model_id: event.target.value, factor: upscale?.factor ?? 2 }
                        : null,
                    )
                  }
                >
                  <option value="">No upscale</option>
                  {model?.upscalers?.map((available) => (
                    <option key={available.id} value={available.id}>
                      {available.display_name}
                    </option>
                  ))}
                </select>
              </label>
              {upscale && (
                <label>
                  Upscale factor
                  <select
                    aria-label="Upscale factor"
                    value={upscale.factor}
                    onChange={(event) =>
                      setUpscale((current) =>
                        current
                          ? { ...current, factor: Number(event.target.value) as 2 | 4 }
                          : null,
                      )
                    }
                  >
                    <option value={2}>2×</option>
                    <option value={4}>4×</option>
                  </select>
                </label>
              )}
            </fieldset>
          )}
          {(mode === "img2img" || mode === "fill") && (
            <ImageInputField
              label="Source image"
              purpose="init"
              value={initImageId}
              onChange={setInitImageId}
            />
          )}
          {mode === "fill" && (
            <ImageInputField
              label="Mask image"
              purpose="mask"
              value={maskId}
              onChange={setMaskId}
            />
          )}
          {mode === "img2img" && (
            <label>
              Image strength
              <input
                aria-label="Image strength"
                type="number"
                min="0.000001"
                max="1"
                step="any"
                value={strength ?? ""}
                onChange={(event) => setStrength(event.target.value || null)}
              />
            </label>
          )}
          {mode === "control" && (
            <fieldset>
              <legend>Canny control</legend>
              <label>
                Control model
                <select
                  aria-label="Control model"
                  value={controlModelId ?? ""}
                  onChange={(event) => setControlModelId(event.target.value || null)}
                >
                  <option value="">Choose control model</option>
                  {model?.controls?.map((available) => (
                    <option key={available.id} value={available.id}>
                      {available.display_name}
                    </option>
                  ))}
                </select>
              </label>
              <ImageInputField
                label="Control image"
                purpose="control"
                value={controlImageId}
                onChange={setControlImageId}
              />
              <label>
                Control strength
                <input
                  aria-label="Control strength"
                  type="number"
                  min="0.000001"
                  max="1"
                  step="any"
                  value={controlStrength}
                  onChange={(event) => setControlStrength(event.target.value)}
                />
              </label>
              <label>
                Canny low threshold
                <input
                  aria-label="Canny low threshold"
                  type="number"
                  min={0}
                  max={254}
                  value={lowThreshold}
                  onChange={(event) => setLowThreshold(Number(event.target.value))}
                />
              </label>
              <label>
                Canny high threshold
                <input
                  aria-label="Canny high threshold"
                  type="number"
                  min={1}
                  max={255}
                  value={highThreshold}
                  onChange={(event) => setHighThreshold(Number(event.target.value))}
                />
              </label>
            </fieldset>
          )}
        </div>
      )}
      <label>
        Prompt
        <textarea
          ref={promptRef}
          aria-label="Image prompt"
          value={prompt}
          maxLength={4000}
          required
          onChange={(event) => setPrompt(event.target.value)}
        />
      </label>
      {limits ? (
        <aside aria-label="Image storage and retention">
          <p>
            {limits.output_retention_hours == null
              ? "Images are retained until you delete them, subject to your storage quota."
              : `Images are automatically deleted after ${limits.output_retention_hours} hours.`}
          </p>
          <p>
            Your storage quota: {storageSize(limits.owner_storage_quota_bytes)}. Up to{" "}
            {limits.pending_per_owner} pending jobs and {limits.daily_outputs_per_owner} accepted
            images per UTC day.
          </p>
          <p>
            Generation inputs: {storageSize(limits.generation_input_max_bytes)}; recipe PNGs:{" "}
            {storageSize(limits.recipe_input_max_bytes)}; outputs:{" "}
            {storageSize(limits.output_max_bytes)}.
          </p>
        </aside>
      ) : (
        <p role="alert">Storage and retention policy is unavailable. Refresh before generating.</p>
      )}
      {missingModel && <p role="alert">The source image model is no longer available.</p>}
      {unsupportedMode && <p role="alert">This model does not support the restored image mode.</p>}
      {unsupportedControlLoras && <p role="alert">Control mode cannot use a LoRA stack.</p>}
      {(mode === "img2img" || mode === "fill") && !initImageId && (
        <p role="alert">A source image is required for {mode}.</p>
      )}
      {mode === "fill" && !maskId && <p role="alert">A mask image is required for fill.</p>}
      {mode === "img2img" && !strength && <p role="alert">Image strength is required.</p>}
      {mode === "control" && !controlImageId && (
        <p role="alert">A control image is required for control generation.</p>
      )}
      {mode === "control" && !controlModelId && (
        <p role="alert">A compatible control model is unavailable for selection.</p>
      )}
      {unavailableControl && <p role="alert">The selected control model is no longer available.</p>}
      {invalidControlSettings && <p role="alert">Control strength or Canny thresholds are invalid.</p>}
      {tooManyLoras && <p role="alert">This model no longer supports this LoRA stack.</p>}
      {unavailableLora && <p role="alert">A selected LoRA is no longer available.</p>}
      {unavailableUpscale && <p role="alert">The selected upscale model is no longer available.</p>}
      {upscaleExceedsBounds && <p role="alert">Upscale exceeds the final image size limit.</p>}
      {invalidLoraScale && <p role="alert">Each LoRA scale must be between -2 and 2.</p>}
      <button className="button" type="submit" disabled={cannotSubmit}>
        {disabled ? "Submitting…" : "Generate"}
      </button>
    </form>
  );
}
