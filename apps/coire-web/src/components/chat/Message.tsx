import Markdown from "react-markdown";
import type { ChatMessage } from "../../api/chat";

const safeUrl = (url: string) => {
  if (/^(https?:|mailto:)/i.test(url) || url.startsWith("/") || url.startsWith("#")) return url;
  return "";
};

export function Message({ message }: { message: ChatMessage }) {
  return (
    <article className={"chat-message " + message.role}>
      <div className="chat-message-label">
        {message.role === "user" ? "You" : message.model_display_name || "Assistant"}
      </div>
      <div className="chat-message-body">
        <Markdown
          urlTransform={safeUrl}
          components={{
            img: () => null,
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
    </article>
  );
}
