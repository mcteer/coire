import type { ChatAttachment, ChatMessage } from "../../api/chat";
import { Message } from "./Message";
import { CoireMark, Icon } from "../Icons";
import type { ReactNode } from "react";

export function MessageList({
  messages,
  attachments,
  feedback,
  onPromptSelect,
}: {
  messages: ChatMessage[];
  attachments: ChatAttachment[];
  feedback?: (message: ChatMessage) => ReactNode;
  onPromptSelect?: (prompt: string) => void;
}) {
  if (!messages.length)
    return (
      <section className="chat-welcome" aria-label="Start a conversation">
        <div className="chat-welcome-mark">
          <CoireMark size={48} />
        </div>
        <div className="eyebrow">Your workspace, your ideas</div>
        <h2>What would you like to explore?</h2>
        <p>Choose a model, then start a conversation.</p>
        {onPromptSelect && (
          <div className="chat-suggestions">
            {[
              { title: "Understand something", text: "Explain a complex idea in simple terms." },
              {
                title: "Find a new perspective",
                text: "Help me explore a problem from a different perspective.",
              },
              { title: "Make a plan", text: "Help me turn an idea into an actionable plan." },
            ].map((item) => (
              <button type="button" key={item.title} onClick={() => onPromptSelect(item.text)}>
                <span>{item.title}</span>
                <Icon name="chevron" size={14} />
              </button>
            ))}
          </div>
        )}
      </section>
    );
  return (
    <div className="chat-messages" aria-label="Conversation">
      {messages.map((message) => (
        <Message key={message.id} message={message} attachments={attachments}>
          {feedback?.(message)}
        </Message>
      ))}
    </div>
  );
}
