import { Composer } from "../components/chat/Composer";
import { ConversationHistory } from "../components/chat/ConversationHistory";
import { MessageList } from "../components/chat/MessageList";
import { ModelPicker } from "../components/chat/ModelPicker";
import { useConversation } from "../hooks/useConversation";
import "../styles/chat.css";

export function Chat({ ownerId }: { ownerId: string }) {
  return <ChatSession key={ownerId} ownerId={ownerId} />;
}

function ChatSession({ ownerId }: { ownerId: string }) {
  const chat = useConversation(ownerId);
  if (!chat.available)
    return (
      <main className="chat-page">
        <section className="panel glass">
          <h1>Chat is unavailable</h1>
          <p>This deployment has not enabled Chat yet. Please try again later.</p>
        </section>
      </main>
    );
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
          onClick={() => void chat.newConversation()}
          disabled={chat.active && !chat.conversation?.active_turn_id}
        >
          New conversation
        </button>
      </div>
      <div className="chat-layout">
        <ConversationHistory
          conversations={chat.history}
          selectedId={chat.conversation?.id ?? null}
          active={chat.active && !chat.conversation?.active_turn_id}
          loading={chat.historyLoading}
          hasMore={Boolean(chat.historyCursor)}
          onOpen={(id) => void chat.openConversation(id)}
          onMore={() => void chat.moreConversations()}
          onRename={chat.rename}
          onDelete={chat.remove}
        />
        <div className="chat-thread">
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
          {chat.olderPosition && (
            <button
              className="button"
              type="button"
              onClick={() => void chat.moreMessages()}
              disabled={chat.historyLoading}
            >
              Load older messages
            </button>
          )}
          <MessageList messages={chat.messages} />
          <Composer
            value={chat.draft}
            onChange={chat.setDraft}
            onSend={() => void chat.send()}
            disabled={!chat.selectedId || chat.active || Boolean(chat.conversation?.active_turn_id)}
            status={chat.status}
          />
          {chat.conversation?.active_turn_id && (
            <button
              className="button"
              type="button"
              onClick={() => void chat.stop()}
              disabled={chat.stopPending}
            >
              {chat.stopPending ? "Stopping…" : "Stop response"}
            </button>
          )}
        </div>
      </div>
    </main>
  );
}
