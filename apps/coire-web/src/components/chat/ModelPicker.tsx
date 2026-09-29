import type { ChatPickerEntry } from "../../api/chat";

function contextLabel(value: number | null | undefined): string {
  if (value == null) return "Context unknown";
  return value >= 1000 ? Math.round(value / 1000) + "k context" : value + " context";
}

export function ModelPicker({
  models,
  selectedId,
  onSelect,
  disabled = false,
}: {
  models: ChatPickerEntry[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  disabled?: boolean;
}) {
  if (models.length === 0)
    return (
      <p className="empty">
        No chat models are available to your account. Ask an administrator to publish a ready model.
      </p>
    );
  const groups = new Map<string, ChatPickerEntry[]>();
  for (const model of models) {
    const group = model.tags?.[0] ?? "general";
    groups.set(group, [...(groups.get(group) ?? []), model]);
  }
  return (
    <section className="chat-picker" aria-label="Choose a model">
      {[...groups].map(([group, entries]) => (
        <div key={group}>
          <h3>{group[0]?.toUpperCase() + group.slice(1)}</h3>
          <div className="chat-model-grid">
            {entries.map((model) => (
              <button
                type="button"
                key={model.id}
                className={"chat-model glass " + (selectedId === model.id ? "selected" : "")}
                aria-pressed={selectedId === model.id}
                disabled={disabled}
                onClick={() => onSelect(model.id)}
              >
                <strong>{model.display_name}</strong>
                <small>
                  {model.source === "studio"
                    ? "Studio"
                    : model.source === "openai"
                      ? "OpenAI"
                      : "Anthropic"}
                </small>
                {model.description && <span>{model.description}</span>}
                <small>
                  {model.tags?.join(" · ") || "General"} · {contextLabel(model.context_window)} ·{" "}
                  {model.size_class}
                </small>
                <small className="chat-load">
                  {model.source !== "studio"
                    ? "Remote · ready"
                    : model.load_state === "cold"
                      ? model.estimated_warmup_seconds == null
                        ? "Cold · warm-up estimate unavailable"
                        : "Cold · about " + Math.ceil(model.estimated_warmup_seconds) + " s warm-up"
                      : model.load_state === "loading"
                        ? "Warming up"
                        : "Ready to chat"}
                </small>
              </button>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}
