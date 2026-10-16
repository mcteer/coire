import { useEffect, useRef, useState } from "react";
import { downloadImageOutput, type ImageOutput } from "../../api/images";
import { ConfirmAction } from "../ConfirmAction";
import { Icon } from "../Icons";
import "../../styles/images.css";

const tags = ["normal", "explicit", "unknown"] as const;
const classifierDiagnostics: Record<NonNullable<ImageOutput["classifier_diagnostic"]>, string> = {
  classifier_unavailable: "Classifier unavailable; tag is unverified.",
  classifier_failed: "Classifier failed; tag is unverified.",
  classifier_timeout: "Classifier timed out; tag is unverified.",
  classifier_memory: "Classifier exceeded its memory limit; tag is unverified.",
  classifier_invalid_result: "Classifier returned an invalid result; tag is unverified.",
};

function Thumbnail({
  outputId,
  onOpen,
}: {
  outputId: string;
  onOpen?: (trigger: HTMLButtonElement) => void;
}) {
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
        onOpen ? (
          <button
            type="button"
            className="image-open-preview"
            aria-label={`Preview image ${outputId}`}
            onClick={(event) => onOpen(event.currentTarget)}
          >
            <img src={url} alt="Generated image" />
          </button>
        ) : (
          <img src={url} alt="Generated image enlarged" />
        )
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
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<ImageOutput | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const previewTrigger = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    const visible = new Set(outputs.map((output) => output.id));
    setSelected((current) => {
      const next = new Set([...current].filter((id) => visible.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [outputs]);
  useEffect(() => {
    if (!preview) return;
    dialog.current?.showModal();
    const trigger = previewTrigger.current;
    return () => {
      window.requestAnimationFrame(() => trigger?.focus());
    };
  }, [preview]);
  const deleteSelected = async () => {
    setDeleting(true);
    setDeleteError(null);
    const ids = outputs.filter((output) => selected.has(output.id)).map((output) => output.id);
    const results = await Promise.allSettled(ids.map(onDelete));
    const failed = ids.filter((_, index) => results[index].status === "rejected");
    setSelected(new Set(failed));
    if (failed.length)
      setDeleteError(`${failed.length} images could not be deleted. Select delete again to retry.`);
    setDeleting(false);
  };
  return (
    <section
      id="image-library"
      tabIndex={-1}
      className="panel glass wide image-gallery"
      aria-label="Image library"
    >
      <div className="image-gallery-header">
        <h2>Library</h2>
        <div className="image-selection-actions">
          <button
            className="button"
            type="button"
            onClick={() => setSelected(new Set(outputs.map((output) => output.id)))}
            disabled={!outputs.length || deleting}
          >
            Select all
          </button>
          {selected.size > 0 && (
            <>
              <span role="status">
                {outputs.filter((output) => selected.has(output.id)).length} selected
              </span>
              <button
                className="button"
                type="button"
                disabled={deleting}
                onClick={() => setSelected(new Set())}
              >
                Clear selection
              </button>
              {!deleting && (
                <ConfirmAction
                  target={`${outputs.filter((output) => selected.has(output.id)).length} selected images permanently`}
                  label="Delete selected"
                  onConfirm={deleteSelected}
                />
              )}
              {deleting && <span role="status">Deleting images…</span>}
            </>
          )}
        </div>
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
              <div className="image-thumbnail-stage">
                <Thumbnail
                  outputId={output.id}
                  onOpen={(trigger) => {
                    previewTrigger.current = trigger;
                    setPreview(output);
                  }}
                />
                <label className="image-select-control" title="Select image">
                  <input
                    type="checkbox"
                    aria-label={`Select image ${output.id}`}
                    checked={selected.has(output.id)}
                    disabled={deleting}
                    onChange={(event) =>
                      setSelected((current) => {
                        const next = new Set(current);
                        if (event.target.checked) next.add(output.id);
                        else next.delete(output.id);
                        return next;
                      })
                    }
                  />
                </label>
                <div className="image-thumbnail-tools">
                  <button
                    type="button"
                    title="Reuse settings"
                    aria-label={`Reuse settings from image ${output.id}`}
                    onClick={() => onReuse(output)}
                  >
                    <Icon name="reuse" />
                  </button>
                  <button
                    type="button"
                    title="Regenerate"
                    aria-label={`Regenerate image ${output.id}`}
                    onClick={() => onRegenerate(output, false)}
                  >
                    <Icon name="regenerate" />
                  </button>
                  <button
                    type="button"
                    title="New seed"
                    aria-label={`Generate new seed from image ${output.id}`}
                    onClick={() => onRegenerate(output, true)}
                  >
                    <Icon name="plus" />
                  </button>
                  <button
                    type="button"
                    title="Download"
                    disabled={busyId === output.id}
                    aria-label={`Download image ${output.id}`}
                    onClick={() => onDownload(output)}
                  >
                    <Icon name="download" />
                  </button>
                  <ConfirmAction
                    target={output.id.slice(0, 8)}
                    label="Delete"
                    ariaLabel={`Delete image ${output.id}`}
                    icon={<Icon name="trash" />}
                    onConfirm={() => onDelete(output.id)}
                  />
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {deleteError && (
        <p className="error" role="alert">
          {deleteError}
        </p>
      )}
      {preview && (
        <dialog
          ref={dialog}
          className="image-preview-dialog"
          aria-label="Image preview"
          onCancel={() => setPreview(null)}
          onClose={() => setPreview(null)}
          onKeyDown={(event) => {
            if (event.key !== "Tab") return;
            const buttons = Array.from(
              event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
            );
            const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
            event.preventDefault();
            buttons[
              (current + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length
            ]?.focus();
          }}
        >
          <header>
            <span>Image preview</span>
            <button
              className="button"
              type="button"
              autoFocus
              aria-label="Close image preview"
              onClick={() => setPreview(null)}
            >
              <Icon name="close" />
            </button>
          </header>
          <Thumbnail outputId={preview.id} />
          {preview.classifier_diagnostic && (
            <p role="status">{classifierDiagnostics[preview.classifier_diagnostic]}</p>
          )}
          <footer>
            <time dateTime={preview.created_at}>
              {new Date(preview.created_at).toLocaleString()}
            </time>
            <button
              className="button"
              type="button"
              disabled={busyId === preview.id}
              onClick={() => onDownload(preview)}
            >
              Download image
            </button>
          </footer>
        </dialog>
      )}
      {nextCursor && (
        <button className="button" type="button" onClick={onLoadOlder}>
          Load older images
        </button>
      )}
    </section>
  );
}
