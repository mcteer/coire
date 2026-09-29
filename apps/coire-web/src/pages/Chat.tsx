import { Composer } from "../components/chat/Composer";
import { MessageList } from "../components/chat/MessageList";
import { ModelPicker } from "../components/chat/ModelPicker";
import { useConversation } from "../hooks/useConversation";
import "../styles/chat.css";

export function Chat() {
  const chat = useConversation();
  return (
    <main className="chat-page">
      <div className="chat-topline">
        <div>
          <h1>Chat</h1>
          <p className="muted">Choose a model and ask what is on your mind.</p>
        </div>
        <button
          className="button"
          type="button"
          onClick={chat.newConversation}
          disabled={chat.active}
        >
          New conversation
        </button>
      </div>
      {chat.error && (
        <p className="error" role="alert">
          {chat.error}
        </p>
      )}
      {chat.loading ? (
        <p>Loading available models…</p>
      ) : (
        <ModelPicker
          models={chat.models}
          selectedId={chat.selectedId}
          onSelect={chat.setSelectedId}
          disabled={chat.active}
        />
      )}
      <MessageList messages={chat.messages} />
      <Composer
        value={chat.draft}
        onChange={chat.setDraft}
        onSend={() => void chat.send()}
        disabled={!chat.selectedId || chat.active}
        status={chat.status}
      />
    </main>
  );
}
