import { useEffect, useRef, useState } from "react";
import {
  getImageInput,
  importImageRecipe,
  uploadImageRecipe,
  type ImageRecipeImport,
  type ImageSubmitRequest,
} from "../../api/images";
import { ImageInputField } from "./ImageInputField";

const pollDelayMs = 500;
const pollLimit = 60;

export function ImageMetadataImport({
  onImport,
}: {
  onImport: (request: ImageSubmitRequest) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ImageRecipeImport | null>(null);
  const [replacements, setReplacements] = useState<Record<string, string>>({});
  const recipeInputId = useRef<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const select = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError("");
    setResult(null);
    setReplacements({});
    recipeInputId.current = null;
    try {
      const receipt = await uploadImageRecipe(file);
      recipeInputId.current = receipt.id;
      let input = receipt;
      for (let attempt = 0; attempt < pollLimit && input.state === "processing"; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, pollDelayMs));
        if (!mounted.current) return;
        input = await getImageInput(receipt.id);
      }
      if (input.state !== "ready") {
        throw new Error(input.safe_error ?? "Recipe processing did not complete");
      }
      const imported = await importImageRecipe(input.id);
      if (!mounted.current) return;
      setResult(imported);
      if (!imported.missing_input_sha256?.length && !imported.missing_dependency_sha256?.length) {
        onImport(imported.settings);
      }
    } catch (cause) {
      if (mounted.current) setError(String(cause));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  const rebind = async () => {
    if (!result) return;
    setBusy(true);
    setError("");
    try {
      if (!recipeInputId.current) throw new Error("Recipe input is unavailable");
      const imported = await importImageRecipe(recipeInputId.current, {
        schema_version: 1,
        replacement_inputs: replacements,
      });
      if (!mounted.current) return;
      setResult(imported);
      if (!imported.missing_input_sha256?.length && !imported.missing_dependency_sha256?.length) {
        onImport(imported.settings);
      }
    } catch (cause) {
      if (mounted.current) setError(String(cause));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  const sourcePurpose = (digest: string): "init" | "mask" | "control" | null => {
    const source = result?.recipe.resolved.inputs?.find((item) => item.sha256 === digest);
    const spec = result?.recipe.resolved.spec;
    if (!source || !spec) return null;
    if (source.input_id === spec.init_image_id) return "init";
    if (source.input_id === spec.mask_id) return "mask";
    if (source.input_id === spec.control?.image_id) return "control";
    return null;
  };

  return (
    <section aria-label="Import image settings">
      <h2>Import image settings</h2>
      <p>Drop a Coire PNG recipe or choose a file up to 64 MiB. Source images remain separate.</p>
      <label
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          void select(event.dataTransfer.files[0]);
        }}
      >
        Recipe PNG
        <input
          aria-label="Recipe PNG"
          type="file"
          accept="image/png"
          disabled={busy}
          onChange={(event) => void select(event.target.files?.[0])}
        />
      </label>
      {busy && <p role="status">Reading private recipe…</p>}
      {error && <p role="alert">{error}</p>}
      {result && (
        <div>
          <p>
            {result.missing_input_sha256?.length || result.missing_dependency_sha256?.length
              ? "Recipe read. Attach missing assets before restoring settings. "
              : "Settings restored. "}
            Exact reproduction is{" "}
            {result.exact_reproduction_available ? "available" : "unavailable"}.
          </p>
          {result.unavailable_reason && <p>Environment: {result.unavailable_reason}</p>}
          {(result.missing_input_sha256?.length ?? 0) > 0 && (
            <p>Missing source inputs: {result.missing_input_sha256?.join(", ")}</p>
          )}
          {(result.missing_dependency_sha256?.length ?? 0) > 0 && (
            <p>Missing model components: {result.missing_dependency_sha256?.join(", ")}</p>
          )}
          {result.missing_input_sha256?.map((digest) => {
            const purpose = sourcePurpose(digest);
            return purpose ? (
              <ImageInputField
                key={digest}
                label={`Reattach ${purpose} image ${digest.slice(0, 12)}`}
                purpose={purpose}
                value={replacements[digest] ?? null}
                expectedSha256={digest}
                onChange={(id) =>
                  setReplacements((current) => {
                    const next = { ...current };
                    if (id) next[digest] = id;
                    else delete next[digest];
                    return next;
                  })
                }
              />
            ) : (
              <p role="alert" key={digest}>
                Source input binding is unavailable: {digest}
              </p>
            );
          })}
          {(result.missing_input_sha256?.length ?? 0) > 0 && (
            <button
              type="button"
              className="button"
              disabled={
                busy ||
                (result.missing_dependency_sha256?.length ?? 0) > 0 ||
                result.missing_input_sha256?.some((digest) => !replacements[digest])
              }
              onClick={() => void rebind()}
            >
              Restore with reattached inputs
            </button>
          )}
        </div>
      )}
    </section>
  );
}
