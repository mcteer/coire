import { useState } from "react";
import type { ChatConversation } from "../../api/chat";
import { ConfirmAction } from "../ConfirmAction";

export function ConversationHistory({
  conversations,
  selectedId,
  active,
  loading,
  hasMore,
  onOpen,
  onMore,
  onRename,
  onDelete,
}: {
  conversations: ChatConversation[];
  selectedId: string | null;
  active: boolean;
  loading: boolean;
  hasMore: boolean;
  onOpen: (id: string) => void;
  onMore: () => void;
  onRename?: (id: string, title: string, revision: number) => Promise<boolean>;
  onDelete?: (id: string, revision: number) => Promise<boolean>;
}) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [title, setTitle] = useState("");
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
            <div key={conversation.id} className="chat-history-item">
              <button
                type="button"
                disabled={active}
                aria-current={selectedId === conversation.id ? "page" : undefined}
                onClick={() => {
                  onOpen(conversation.id);
                  setExpanded(false);
                }}
              >
                <span id={`chat-title-${conversation.id}`}>{conversation.title}</span>
                <small>{new Date(conversation.updated_at).toLocaleDateString()}</small>
              </button>
              {onRename && (
                editing === conversation.id ? (
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      if (!title.trim()) return;
                      void onRename(conversation.id, title, conversation.revision).then((saved) => {
                        if (saved) setEditing(null);
                      });
                    }}
                  >
                    <label htmlFor={`chat-rename-${conversation.id}`}>Conversation title</label>
                    <input
                      id={`chat-rename-${conversation.id}`}
                      value={title}
                      maxLength={120}
                      onChange={(event) => setTitle(event.target.value)}
                    />
                    <button type="submit" disabled={!title.trim()}>Save title</button>
                    <button type="button" onClick={() => setEditing(null)}>Cancel</button>
                  </form>
                ) : (
                  <button
                    type="button"
                    aria-label="Rename conversation"
                    aria-describedby={`chat-title-${conversation.id}`}
                    onClick={() => {
                      setEditing(conversation.id);
                      setTitle(conversation.title);
                    }}
                  >
                    Rename
                  </button>
                )
              )}
              {onDelete && (
                <ConfirmAction
                  target={conversation.title}
                  label="Delete"
                  ariaLabel="Delete conversation"
                  onConfirm={async () => {
                    await onDelete(conversation.id, conversation.revision);
                  }}
                />
              )}
            </div>
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
