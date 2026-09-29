import { isValidElement, useState, type ReactNode } from "react";

function plainText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(plainText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return plainText(node.props.children);
  return "";
}

export function CodeBlock({ children }: { children: ReactNode }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(plainText(children).replace(/\n$/, ""));
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };
  return (
    <div className="chat-code-block">
      <button className="button" type="button" onClick={() => void copy()}>
        {copyState === "copied" ? "Copied" : "Copy code"}
      </button>
      {copyState === "failed" && <span role="status">Copy failed</span>}
      <pre>{children}</pre>
    </div>
  );
}
