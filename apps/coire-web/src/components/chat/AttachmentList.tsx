import type { ChangeEvent } from "react";
import {
  chatFileDownloadUrl,
  chatFilePreviewUrl,
  type ChatAttachment,
  type ChatAttachmentSelection,
} from "../../api/chat";

export function AttachmentList({
  conversationId,
  attachments,
  selections,
  busy,
  onUpload,
  onProcess,
  onSelect,
}: {
  conversationId: string | null;
  attachments: ChatAttachment[];
  selections: ChatAttachmentSelection[];
  busy: boolean;
  onUpload: (file: File) => void;
  onProcess: (fileId: string, operation: "inspect" | "render", pages?: number[]) => void;
  onSelect: (selection: ChatAttachmentSelection | null, fileId: string) => void;
}) {
  const chooseFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) onUpload(file);
    event.target.value = "";
  };
  return (
    <section className="chat-files glass" aria-label="Conversation files">
      <div className="chat-files-heading">
        <strong>Files</strong>
        <label className="button" htmlFor="chat-file-upload">
          Add file
        </label>
        <input
          id="chat-file-upload"
          type="file"
          accept=".txt,.md,.csv,.json,.py,.ts,.tsx,.js,.jsx,.rs,.go,.pdf,image/png,image/jpeg,image/webp"
          onChange={chooseFile}
          disabled={busy}
        />
      </div>
      <p className="muted">Text, code, PDFs and PNG, JPEG or WebP images. Up to 10 MiB each.</p>
      {attachments.length === 0 && <p className="muted">No files in this conversation.</p>}
      <ul className="chat-file-list">
        {attachments.map((attachment) => {
          const selected = selections.find((item) => item.file_id === attachment.id);
          const pdf = attachment.detected_type === "application/pdf";
          const image = attachment.detected_type.startsWith("image/");
          const canSelect = attachment.state === "ready";
          const mode = selected?.mode ?? (image ? "visual" : "text");
          const pages = selected?.pages ?? [];
          const pageCount = attachment.page_count ?? 0;
          return (
            <li key={attachment.id}>
              <div className="chat-file-row">
                <label>
                  <input
                    type="checkbox"
                    checked={Boolean(selected)}
                    disabled={!canSelect || busy || (!selected && selections.length >= 10)}
                    onChange={(event) =>
                      onSelect(
                        event.target.checked ? { file_id: attachment.id, mode } : null,
                        attachment.id,
                      )
                    }
                  />
                  <span>{attachment.filename}</span>
                </label>
                {conversationId && (
                  <a href={chatFileDownloadUrl(conversationId, attachment.id)} download>
                    Download
                  </a>
                )}
              </div>
              <small>
                {attachment.state === "processing"
                  ? "Processing…"
                  : attachment.state === "failed"
                    ? `Failed: ${attachment.safe_error ?? "processing failed"}`
                    : attachment.state === "ready"
                      ? `Ready${pdf ? ` · ${pageCount} pages` : ""}`
                      : attachment.state}
              </small>
              {attachment.state === "failed" && (
                <button
                  className="button"
                  type="button"
                  disabled={busy}
                  onClick={() => onProcess(attachment.id, "inspect")}
                >
                  Retry processing
                </button>
              )}
              {selected && pdf && (
                <div className="chat-file-pages">
                  <label>
                    Content mode
                    <select
                      aria-label={`Content mode for ${attachment.filename}`}
                      value={mode}
                      disabled={busy}
                      onChange={(event) =>
                        onSelect(
                          {
                            file_id: attachment.id,
                            mode: event.target.value === "visual" ? "visual" : "text",
                            pages: event.target.value === "visual" ? pages : [],
                          },
                          attachment.id,
                        )
                      }
                    >
                      <option value="text">Extracted text</option>
                      <option value="visual">Selected page images</option>
                    </select>
                  </label>
                  {mode === "visual" && (
                    <>
                      <fieldset>
                        <legend>Pages to include (up to ten)</legend>
                        {Array.from({ length: pageCount }, (_, index) => index + 1).map((page) => (
                          <label key={page}>
                            <input
                              type="checkbox"
                              checked={pages.includes(page)}
                              disabled={busy || (!pages.includes(page) && pages.length >= 10)}
                              onChange={(event) =>
                                onSelect(
                                  {
                                    file_id: attachment.id,
                                    mode: "visual",
                                    pages: event.target.checked
                                      ? [...pages, page].sort((a, b) => a - b)
                                      : pages.filter((value) => value !== page),
                                  },
                                  attachment.id,
                                )
                              }
                            />
                            {page}
                          </label>
                        ))}
                      </fieldset>
                      <p role="status">Included pages: {pages.join(", ") || "none selected"}</p>
                      <button
                        className="button"
                        type="button"
                        disabled={busy || pages.length === 0}
                        onClick={() => onProcess(attachment.id, "render", pages)}
                      >
                        Prepare selected pages
                      </button>
                    </>
                  )}
                </div>
              )}
              {conversationId && (attachment.previews ?? []).length > 0 && (
                <div className="chat-file-previews">
                  {(attachment.previews ?? []).map((preview) => (
                    <figure key={preview.id}>
                      <img
                        src={chatFilePreviewUrl(conversationId, attachment.id, preview.id)}
                        alt={
                          preview.page
                            ? `Page ${preview.page} of ${attachment.filename}`
                            : attachment.filename
                        }
                        loading="lazy"
                      />
                      {preview.page && <figcaption>Page {preview.page}</figcaption>}
                    </figure>
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
