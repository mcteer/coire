import { useState } from "react";
import { qualifyImageResolution } from "../../api/images";
import { ConfirmAction } from "../ConfirmAction";
export function ImageResolutionQualification({
  modelId,
  name,
  onChanged,
}: {
  modelId: string;
  name: string;
  onChanged: () => void | Promise<void>;
}) {
  const [job, setJob] = useState<string | null>(null),
    [error, setError] = useState("");
  return (
    <div className="image-qualification">
      <ConfirmAction
        target={name}
        label="Qualify larger image sizes"
        onConfirm={async () => {
          setError("");
          try {
            const receipt = await qualifyImageResolution(modelId);
            setJob(receipt.job_id);
            await onChanged();
          } catch (cause) {
            setError(String(cause));
          }
        }}
      />
      {job && (
        <p role="status">
          Resolution qualification submitted. Follow acquisition status; current limits remain until
          both Studios pass.
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
