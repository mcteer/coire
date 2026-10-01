import { useEffect, useState } from "react";
import { downloadImageOutput, type ImageOutput } from "../../api/images";
import { ConfirmAction } from "../ConfirmAction";
import "../../styles/images.css";

const tags = ["normal", "explicit", "unknown"] as const;

function Thumbnail({ outputId }: { outputId: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let objectUrl = "";
    let cancelled = false;
    setFailed(false);
    void downloadImageOutput(outputId)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) {
          setUrl(null);
          setFailed(true);
        }
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [outputId, attempt]);
  return (
    <div className="image-gallery-thumb">
      {url ? (
        <img src={url} alt="" />
      ) : failed ? (
        <div className="image-gallery-thumb-message">
          <span>Preview unavailable</span>
          <button type="button" className="button" onClick={() => setAttempt((n) => n + 1)}>
            Retry preview
          </button>
        </div>
      ) : (
        <span role="status">Loading preview…</span>
      )}
    </div>
  );
}

export function ImageGallery({
  outputs,
  loading,
  tag,
  onTagChange,
  nextCursor,
  busyId,
  onDownload,
  onDelete,
  onLoadOlder,
  onReuse,
  onRegenerate,
}: {
  outputs: ImageOutput[];
  loading: boolean;
  tag: ImageOutput["tag"] | "";
  onTagChange: (tag: ImageOutput["tag"] | "") => void;
  nextCursor: string | null;
  busyId: string | null;
  onDownload: (output: ImageOutput) => void;
  onDelete: (outputId: string) => Promise<void>;
  onLoadOlder: () => void;
  onReuse: (output: ImageOutput) => void;
  onRegenerate: (output: ImageOutput, newSeed: boolean) => void;
}) {
  return (
    <section className="panel glass wide image-gallery" aria-label="Image library">
      <div className="image-gallery-header">
        <h2>Library</h2>
        <label>
          Tag
          <select
            aria-label="Image tag"
            value={tag}
            onChange={(event) => onTagChange(event.target.value as ImageOutput["tag"] | "")}
          >
            <option value="">All tags</option>
            {tags.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
      </div>
      {loading ? <p>Loading images…</p> : outputs.length === 0 ? <p>No images yet.</p> : null}
      {outputs.length > 0 && (
        <ul className="image-gallery-grid">
          {outputs.map((output) => (
            <li className="image-gallery-card" key={output.id}>
              <Thumbnail outputId={output.id} />
              <div className="image-gallery-card-body">
                <p className="image-gallery-meta">
                  <span className="mono">{output.id.slice(0, 8)}</span>
                  <span className="image-gallery-tag">{output.tag}</span>
                </p>
                <time dateTime={output.created_at}>
                  {new Date(output.created_at).toLocaleString()}
                </time>
                <div className="image-gallery-actions">
                  <button
                    className="button"
                    type="button"
                    onClick={() => onReuse(output)}
                    aria-label={`Reuse settings from image ${output.id}`}
                  >
                    Reuse settings
                  </button>
                  <button
                    className="button"
                    type="button"
                    onClick={() => onRegenerate(output, false)}
                    aria-label={`Regenerate image ${output.id}`}
                  >
                    Regenerate
                  </button>
                  <button
                    className="button"
                    type="button"
                    onClick={() => onRegenerate(output, true)}
                    aria-label={`Generate new seed from image ${output.id}`}
                  >
                    New seed
                  </button>
                  <button
                    className="button"
                    type="button"
                    disabled={busyId === output.id}
                    onClick={() => onDownload(output)}
                    aria-label={`Download image ${output.id}`}
                  >
                    {busyId === output.id ? "Downloading…" : "Download"}
                  </button>
                  <ConfirmAction
                    target={output.id.slice(0, 8)}
                    label="Delete"
                    ariaLabel={`Delete image ${output.id}`}
                    onConfirm={() => onDelete(output.id)}
                  />
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {nextCursor && (
        <button className="button" type="button" onClick={onLoadOlder}>
          Load older images
        </button>
      )}
    </section>
  );
}
