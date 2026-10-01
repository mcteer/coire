import { useEffect, useRef, useState } from "react";
import { getImageInput, uploadImageInput, type ImageInput } from "../../api/images";

type Purpose = Exclude<ImageInput["purpose"], "recipe">;

export function ImageInputField({
  label,
  purpose,
  value,
  onChange,
  expectedSha256,
}: {
  label: string;
  purpose: Purpose;
  value: string | null;
  onChange: (inputId: string | null) => void;
  expectedSha256?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError("");
    onChange(null);
    try {
      let input = await uploadImageInput(file, purpose);
      for (
        let count = 0;
        count < 60 && ["uploading", "processing"].includes(input.state);
        count += 1
      ) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        if (!mounted.current) return;
        input = await getImageInput(input.id);
      }
      if (input.state !== "ready" || input.purpose !== purpose) {
        throw new Error(input.safe_error ?? `${label} could not be prepared`);
      }
      if (expectedSha256 && input.sha256 !== expectedSha256) {
        throw new Error("Image digest differs from the saved recipe");
      }
      if (mounted.current) onChange(input.id);
    } catch (cause) {
      if (mounted.current) setError(String(cause));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  return (
    <div>
      <label>
        {label}
        <input
          aria-label={label}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          disabled={busy}
          onChange={(event) => void upload(event.target.files?.[0])}
        />
      </label>
      {busy && <p role="status">Preparing {label.toLowerCase()}…</p>}
      {value && <p>{label} ready</p>}
      {error && (
        <p role="alert">
          {label}: {error}
        </p>
      )}
      <p>
        Private inputs count toward your storage quota until deleted. Deleting an input used by an
        active job requests cancellation; bytes are removed after that job stops.
      </p>
    </div>
  );
}
