import { Icon } from "../Icons";
import type { FormEvent } from "react";

export function Composer({
  value,
  onChange,
  onSend,
  disabled,
  status,
  mode = "chat",
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  disabled: boolean;
  status: string | null;
  mode?: "chat" | "code";
}) {
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!disabled && value.trim()) onSend();
  };
  return (
    <form className="chat-composer glass" onSubmit={submit}>
      <label htmlFor="chat-input">{mode === "code" ? "Task" : "Message"}</label>
      <textarea
        id="chat-input"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={mode === "code" ? "Describe the repository task" : "Ask a question"}
        rows={3}
        maxLength={64 * 1024}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (!disabled && value.trim()) onSend();
          }
        }}
      />
      <div className="chat-composer-bottom">
        <span role="status" aria-label="Response status" aria-live="polite">
          {status || "Enter to send · Shift + Enter for a new line"}
        </span>
        <button className="button" type="submit" disabled={disabled || !value.trim()}>
          Send <Icon name="arrow" size={16} />
        </button>
      </div>
    </form>
  );
}
