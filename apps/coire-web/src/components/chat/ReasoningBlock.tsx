export function ReasoningBlock({ text }: { text: string }) {
  if (!text) return null;
  return (
    <details className="chat-reasoning">
      <summary>Reasoning</summary>
      <div className="chat-reasoning-content">{text}</div>
    </details>
  );
}
