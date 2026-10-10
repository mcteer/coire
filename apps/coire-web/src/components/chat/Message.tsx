import Markdown from "react-markdown";
import type { ReactNode } from "react";
import type { ChatAttachment, ChatMessage } from "../../api/chat";
import { CodeBlock } from "./CodeBlock";
import { ReasoningBlock } from "./ReasoningBlock";

const safeUrl = (url: string) => {
  if (
    /^(https?:|mailto:)/i.test(url) ||
    (url.startsWith("/") && !url.startsWith("//")) ||
    url.startsWith("#")
  )
    return url;
  return "";
};

export function Message({
  message,
  attachments = [],
  children,
}: {
  message: ChatMessage;
  attachments?: ChatAttachment[];
  children?: ReactNode;
}) {
  return (
    <article className={"chat-message " + message.role}>
      <div className="chat-message-label">
        {message.role === "user" ? "You" : message.model_display_name || "Assistant"}
      </div>
      {message.role === "assistant" && <ReasoningBlock text={message.reasoning} />}
      <div className="chat-message-body">
        <Markdown
          urlTransform={safeUrl}
          components={{
            img: () => null,
            pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
            a: ({ href, children }) =>
              href ? (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              ) : (
                <span>{children}</span>
              ),
          }}
        >
          {message.text}
        </Markdown>
      </div>
      {message.role === "user" && (message.attachment_selections ?? []).length > 0 && (
        <ul className="chat-message-files" aria-label="Attached files">
          {(message.attachment_selections ?? []).map((selection) => {
            const file = attachments.find((item) => item.id === selection.file_id);
            return (
              <li key={selection.file_id}>
                {file?.filename ?? "File unavailable"} ·{" "}
                {selection.mode === "text"
                  ? "Extracted text"
                  : `Page images ${selection.pages?.join(", ") || "none"}`}
              </li>
            );
          })}
        </ul>
      )}
      {children}
    </article>
  );
}
