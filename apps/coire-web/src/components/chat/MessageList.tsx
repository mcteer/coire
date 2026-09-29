import type { ChatMessage } from "../../api/chat";
import { Message } from "./Message";

export function MessageList({ messages }: { messages: ChatMessage[] }) {
  if (!messages.length)
    return <p className="chat-intro">Choose a model, then start a conversation.</p>;
  return (
    <div className="chat-messages" aria-label="Conversation">
      {messages.map((message) => (
        <Message key={message.id} message={message} />
      ))}
    </div>
  );
}
