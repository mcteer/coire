import type { ChatAttachment, ChatMessage } from "../../api/chat";
import { Message } from "./Message";
import type { ReactNode } from "react";

export function MessageList({
  messages,
  attachments,
  feedback,
}: {
  messages: ChatMessage[];
  attachments: ChatAttachment[];
  feedback?: (message: ChatMessage) => ReactNode;
}) {
  if (!messages.length)
    return <p className="chat-intro">Choose a model, then start a conversation.</p>;
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
