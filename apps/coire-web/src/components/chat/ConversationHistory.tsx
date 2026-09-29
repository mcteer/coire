import { useState } from "react";
import type { ChatConversation } from "../../api/chat";

export function ConversationHistory({
  conversations,
  selectedId,
  active,
  loading,
  hasMore,
  onOpen,
  onMore,
}: {
  conversations: ChatConversation[];
  selectedId: string | null;
  active: boolean;
  loading: boolean;
  hasMore: boolean;
  onOpen: (id: string) => void;
  onMore: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="chat-history">
      <button
        className="button chat-history-toggle"
        type="button"
        aria-controls="chat-history-list"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        Conversations
      </button>
      <aside
        id="chat-history-list"
        className={"chat-history-panel glass " + (expanded ? "open" : "")}
        aria-label="Conversation history"
      >
        <h2>Conversations</h2>
        {loading && <p>Loading history…</p>}
        {!loading && conversations.length === 0 && (
          <p className="empty">Your conversations will appear here.</p>
        )}
        <div className="chat-history-items">
          {conversations.map((conversation) => (
            <button
              type="button"
              key={conversation.id}
              disabled={active}
              aria-current={selectedId === conversation.id ? "page" : undefined}
              onClick={() => {
                onOpen(conversation.id);
                setExpanded(false);
              }}
            >
              <span>{conversation.title}</span>
              <small>{new Date(conversation.updated_at).toLocaleDateString()}</small>
            </button>
          ))}
        </div>
        {hasMore && (
          <button className="button" type="button" onClick={onMore} disabled={loading || active}>
            Load older conversations
          </button>
        )}
      </aside>
    </div>
  );
}
