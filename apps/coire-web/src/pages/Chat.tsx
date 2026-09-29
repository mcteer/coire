import { useState } from "react";
import { Composer } from "../components/chat/Composer";
import { AttachmentList } from "../components/chat/AttachmentList";
import { ConversationHistory } from "../components/chat/ConversationHistory";
import { MessageList } from "../components/chat/MessageList";
import { ModelPicker } from "../components/chat/ModelPicker";
import { CodeControls } from "../components/chat/CodeControls";
import { RunActivity } from "../components/chat/RunActivity";
import { useConversation } from "../hooks/useConversation";
import { AskCoire } from "./admin/AskCoire";
import "../styles/chat.css";

export function Chat({ ownerId, isAdmin = false }: { ownerId: string; isAdmin?: boolean }) {
  return <ChatSession key={ownerId} ownerId={ownerId} isAdmin={isAdmin} />;
}

function ChatSession({ ownerId, isAdmin }: { ownerId: string; isAdmin: boolean }) {
  const [platformMode, setPlatformMode] = useState(false);
  const chat = useConversation(ownerId);
  const latestCodeTurn = chat.turns.filter((turn) => turn.action !== "chat").at(-1);
  if (!chat.available)
    return (
      <main className="chat-page">
        <section className="panel glass">
          <h1>Chat is unavailable</h1>
          <p>This deployment has not enabled Chat yet. Please try again later.</p>
        </section>
      </main>
    );
  if (chat.reauthRequired)
    return (
      <main className="chat-page">
        <section className="panel glass" role="alert">
          <h1>Sign in again</h1>
          <p>Your session expired. Your draft is saved in this tab for the same account.</p>
          <a className="button" href="/">
            Sign in again
          </a>
        </section>
      </main>
    );
  if (platformMode && isAdmin)
    return (
      <main className="chat-page">
        <div className="chat-topline">
          <div>
            <h1>Chat · Platform</h1>
            <p className="muted">Ask about live status and review proposed changes.</p>
          </div>
          <button className="button" type="button" onClick={() => setPlatformMode(false)}>
            Back to chat
          </button>
        </div>
        <AskCoire />
      </main>
    );
  return (
    <main className="chat-page">
      <div className="chat-topline">
        <div>
          <h1>Chat</h1>
          <p className="muted">Choose a model and send a message or repository task.</p>
        </div>
        <div className="row">
          {isAdmin && (
            <button className="button" type="button" onClick={() => setPlatformMode(true)}>
              Manage platform
            </button>
          )}
          <button
            className="button"
            type="button"
            onClick={() => void chat.newConversation()}
            disabled={chat.active && !chat.conversation?.active_turn_id}
          >
            New conversation
          </button>
        </div>
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
          <div className="chat-mode-switch glass" role="group" aria-label="Conversation mode">
            {(["chat", "code"] as const).map((value) => (
              <button
                key={value}
                className="button"
                type="button"
                aria-pressed={chat.mode === value}
                disabled={Boolean(chat.conversation) || chat.active}
                onClick={() => chat.setMode(value)}
              >
                {value === "chat" ? "Chat" : "Code"}
              </button>
            ))}
          </div>
          {chat.error && (
            <p className="error" role="alert">
              {chat.error}
            </p>
          )}
          {chat.contextExceeded && (
            <div className="chat-selection-remedy" role="status">
              <p>
                This turn exceeds the model’s context limit. Remove selected files or pages, choose
                a model with a larger context below, or start a new conversation. Your draft is saved.
              </p>
              {chat.selections.some((item) => item.mode === "visual") && (
                <button className="button" type="button" onClick={chat.removeVisualSelections}>
                  Remove visual selections
                </button>
              )}
            </div>
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
          <MessageList messages={chat.messages} attachments={chat.attachments} />
          {chat.mode === "code" && latestCodeTurn && (
            <RunActivity
              key={latestCodeTurn.id}
              conversationId={chat.conversation?.id ?? ""}
              turnId={latestCodeTurn.id}
              activity={chat.activities[latestCodeTurn.id] ?? []}
              status={chat.activityStatus[latestCodeTurn.id]}
              result={chat.codingResults[latestCodeTurn.id]}
            />
          )}
          {chat.retryableTurn && (
            <div className="chat-retry glass">
              <p>The last response ended early. Its partial text remains above.</p>
              <button
                className="button"
                type="button"
                disabled={!chat.selectedId || chat.active || chat.fileBusy}
                onClick={() => void chat.retry()}
              >
                Retry response
              </button>
              {chat.canContinue && (
                <button
                  className="button"
                  type="button"
                  disabled={!chat.selectedId || chat.active || chat.fileBusy}
                  onClick={() => void chat.continueResponse()}
                >
                  Continue from partial answer
                </button>
              )}
            </div>
          )}
          {chat.mode === "code" && (
            <CodeControls
              action={chat.action}
              onAction={chat.setAction}
              workspaces={chat.workspaces}
              workspaceId={chat.workspaceId}
              onWorkspace={chat.setWorkspaceId}
              sourceRevision={chat.sourceRevision}
              onRevision={chat.setSourceRevision}
              researchTurns={chat.researchTurns}
              researchId={chat.researchId}
              onResearch={chat.setResearchId}
              planTurns={chat.planTurns}
              planId={chat.planId}
              onPlan={chat.setPlanId}
              onRegister={chat.addWorkspace}
              disabled={chat.active || Boolean(chat.conversation?.active_turn_id)}
            />
          )}
          {(chat.mode === "chat" || chat.mode === "code") && (
            <AttachmentList
              conversationId={chat.conversation?.id ?? null}
              attachments={chat.attachments}
              selections={chat.selections}
              busy={
                !chat.selectedId ||
                chat.active ||
                chat.fileBusy ||
                Boolean(chat.conversation?.active_turn_id)
              }
              onUpload={(file) => void chat.uploadFile(file)}
              onProcess={(fileId, operation, pages) =>
                void chat.processFile(fileId, operation, pages)
              }
              onSelect={chat.selectFile}
            />
          )}
          {chat.selections.length > 0 && !chat.canSendSelections && (
            <div className="chat-selection-remedy" role="status">
              <p className="muted">{chat.selectionIssue}</p>
              {chat.selections.some((item) => item.mode === "visual") && (
                <button className="button" type="button" onClick={chat.removeVisualSelections}>
                  Remove visual selections
                </button>
              )}
            </div>
          )}
          <Composer
            value={chat.draft}
            onChange={chat.setDraft}
            onSend={() => void chat.send()}
            disabled={
              !chat.selectedId ||
              chat.active ||
              chat.fileBusy ||
              !chat.canSendSelections ||
              !chat.codeReady ||
              Boolean(chat.conversation?.active_turn_id)
            }
            status={chat.status}
            mode={chat.mode}
          />
          {chat.conversation?.active_turn_id && (
            <button
              className="button"
              type="button"
              onClick={() => void chat.stop()}
              disabled={chat.stopPending}
            >
              {chat.stopPending ? "Stopping…" : chat.mode === "code" ? "Stop run" : "Stop response"}
            </button>
          )}
        </div>
      </div>
    </main>
  );
}
