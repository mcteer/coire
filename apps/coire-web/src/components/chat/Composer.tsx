import { Icon } from "../Icons";
import { useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

export function Composer({
  value,
  onChange,
  onSend,
  disabled,
  status,
  mode = "chat",
  files,
  onUpload,
  uploadDisabled = false,
  active = false,
  stopping = false,
  onStop,
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  disabled: boolean;
  status: string | null;
  mode?: "chat" | "code";
  files?: ReactNode;
  onUpload?: (file: File) => void;
  uploadDisabled?: boolean;
  active?: boolean;
  stopping?: boolean;
  onStop?: () => void;
}) {
  const [fileError, setFileError] = useState<string | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const upload = useRef<HTMLInputElement>(null);
  useLayoutEffect(() => {
    if (!input.current) return;
    input.current.style.height = "auto";
    input.current.style.height = `${Math.max(56, Math.min(input.current.scrollHeight, 200))}px`;
  }, [value]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!disabled && !active && value.trim()) {
      onSend();
      input.current?.focus();
    }
  };
  const attach = (files: FileList) => {
    if (!onUpload || uploadDisabled) return;
    if (files.length !== 1) {
      setFileError("Attach one file at a time.");
      return;
    }
    setFileError(null);
    onUpload(files[0]);
    input.current?.focus();
  };
  return (
    <form
      className="chat-composer"
      aria-label="Message composer"
      onSubmit={submit}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) event.preventDefault();
      }}
      onDrop={(event) => {
        if (event.dataTransfer.files.length) {
          event.preventDefault();
          attach(event.dataTransfer.files);
        }
      }}
      onPaste={(event) => {
        if (event.clipboardData.files.length) {
          event.preventDefault();
          attach(event.clipboardData.files);
        }
      }}
    >
      {files}
      {fileError && (
        <p className="composer-upload-status" role="alert">
          {fileError}
        </p>
      )}
      <label htmlFor="chat-input">{mode === "code" ? "Task" : "Message"}</label>
      <textarea
        ref={input}
        id="chat-input"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={
          active
            ? "Write your next message…"
            : mode === "code"
              ? "Describe the repository task…"
              : "Message Coire…"
        }
        rows={2}
        maxLength={64 * 1024}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (!disabled && !active && value.trim()) {
              onSend();
              input.current?.focus();
            }
          }
        }}
      />
      <div className="chat-composer-bottom">
        <div className="chat-composer-tools">
          {onUpload && (
            <>
              <button
                className="composer-tool"
                type="button"
                aria-label="Attach files"
                title="Attach files"
                disabled={uploadDisabled}
                onClick={() => upload.current?.click()}
              >
                <Icon name="paperclip" size={20} />
              </button>
              <input
                ref={upload}
                className="visually-hidden"
                tabIndex={-1}
                type="file"
                aria-label="Add file"
                accept=".txt,.md,.csv,.json,.py,.ts,.tsx,.js,.jsx,.rs,.go,.pdf,image/png,image/jpeg,image/webp"
                disabled={uploadDisabled}
                onChange={(event) => {
                  if (event.target.files?.length) attach(event.target.files);
                  event.target.value = "";
                }}
              />
            </>
          )}
          <span role="status" aria-label="Response status" aria-live="polite">
            {status || (active ? "Working…" : "")}
          </span>
        </div>
        {active && onStop ? (
          <button
            className="composer-send"
            type="button"
            aria-label={mode === "code" ? "Stop run" : "Stop response"}
            title={stopping ? "Stopping…" : "Stop response"}
            disabled={stopping}
            onClick={onStop}
          >
            <Icon name="stop" size={16} />
          </button>
        ) : (
          <button
            className="composer-send"
            type="submit"
            aria-label="Send"
            title="Send message"
            disabled={disabled || !value.trim()}
          >
            <Icon name="arrow" size={19} />
          </button>
        )}
      </div>
      <span className="visually-hidden">Enter to send. Shift + Enter for a new line.</span>
    </form>
  );
}
