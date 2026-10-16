import { useEffect, useState } from "react";
import {
  getConversationFeedback,
  type ConversationFeedbackPage,
  type FeedbackPreference,
} from "../api/feedback";
import { FeedbackSettings } from "../components/chat/FeedbackSettings";
import { FeedbackControls } from "../components/chat/FeedbackControls";
import { Comparison } from "../components/chat/Comparison";
import { Composer } from "../components/chat/Composer";
import { Icon } from "../components/Icons";
import { AttachmentList } from "../components/chat/AttachmentList";
import { ConversationHistory } from "../components/chat/ConversationHistory";
import { MessageList } from "../components/chat/MessageList";
import { ModelPicker } from "../components/chat/ModelPicker";
import { CodeControls } from "../components/chat/CodeControls";
import { RunActivity } from "../components/chat/RunActivity";
import { useChatScroll } from "../hooks/useChatScroll";
import { useConversation } from "../hooks/useConversation";
import { AskCoire } from "./admin/AskCoire";
import "../styles/chat.css";

export function Chat({
  ownerId,
  isAdmin = false,
  requestedTarget,
}: {
  ownerId: string;
  isAdmin?: boolean;
  requestedTarget?: string;
}) {
  return (
    <ChatSession
      key={`${ownerId}:${requestedTarget ?? ""}`}
      ownerId={ownerId}
      isAdmin={isAdmin}
      requestedTarget={requestedTarget}
    />
  );
}

function ChatSession({
  ownerId,
  isAdmin,
  requestedTarget,
}: {
  ownerId: string;
  isAdmin: boolean;
  requestedTarget?: string;
}) {
  const [platformMode, setPlatformMode] = useState(false);
  const [exactConfirmed, setExactConfirmed] = useState(false);
  const chat = useConversation(ownerId);
  const [preference, setPreference] = useState<FeedbackPreference | null>(null);
  const lastMessage = chat.messages.at(-1);
  const scroll = useChatScroll(
    chat.conversation?.id ?? null,
    lastMessage
      ? `${chat.messages.length}:${lastMessage.id}:${lastMessage.text.length}:${lastMessage.reasoning?.length ?? 0}`
      : "",
  );
  const [feedback, setFeedback] = useState<ConversationFeedbackPage | null>(null);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);
  const cid = chat.conversation?.id;
  const feedbackCursor = chat.messages
    .filter((message) => message.role === "assistant")
    .slice(-100)[0]?.position;
  const comparisonStamp = chat.comparisons
    .map((row) => `${row.id}:${row.version}:${row.state}:${row.selection_state}`)
    .join(",");
  const pendingComparison = chat.comparisons.some((row) => row.selection_state === "pending");
  const latestTurnState = chat.turns.at(-1)?.state;
  useEffect(() => {
    let current = true;
    if (!cid || !preference || !feedbackCursor) {
      setFeedback(null);
      return;
    }
    void getConversationFeedback(cid, String(Math.max(1, feedbackCursor - 1)))
      .then((page) => {
        if (current) {
          setFeedback(page);
          setFeedbackError(null);
        }
      })
      .catch(() => {
        if (current) {
          setFeedback(null);
          setFeedbackError("Feedback is unavailable. You can continue chatting.");
        }
      });
    return () => {
      current = false;
    };
  }, [cid, preference, feedbackCursor, latestTurnState, comparisonStamp]);
  const refreshFeedback = async () => {
    try {
      await chat.refreshFeedback();
      if (cid)
        setFeedback(
          await getConversationFeedback(
            cid,
            feedbackCursor ? String(Math.max(1, feedbackCursor - 1)) : null,
          ),
        );
    } catch {
      setFeedbackError("Could not refresh feedback. Reload the conversation before trying again.");
    }
  };
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
  if (requestedTarget && !exactConfirmed) {
    const exact = chat.models.find(
      (model) => model.id === requestedTarget && model.target?.adapter_id,
    );
    return (
      <main className="chat-page">
        <section className="panel glass">
          <h1>Exact adapter selection</h1>
          <p className="mono">{requestedTarget}</p>
          {chat.loading ? (
            <p role="status">Checking entitled registry targets…</p>
          ) : exact ? (
            <>
              <p>
                {exact.display_name} ·{" "}
                {exact.verified ? "independently verified" : "unverified — write tasks unavailable"}
              </p>
              <button
                className="button"
                onClick={() => {
                  chat.setSelectedId(exact.id);
                  setExactConfirmed(true);
                }}
              >
                Use this exact registered adapter
              </button>
            </>
          ) : (
            <p role="alert">
              This exact adapter is unavailable in the native Chat picker. Its publication,
              entitlement or native routing capability must be available before selection. No base
              model has been substituted.
            </p>
          )}
          <a href="#chat">Back to Chat</a>
        </section>
      </main>
    );
  }
  return (
    <main className="chat-page">
      <div className="chat-topline">
        <div>
          <h1>Chat</h1>
          <p className="muted">A space for questions, ideas, and the work ahead.</p>
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
      <details className="chat-privacy-disclosure">
        <summary>Feedback &amp; privacy</summary>
        <FeedbackSettings
          refreshKey={`${cid ?? ""}:${chat.comparisons.filter((row) => row.selection_state === "withdrawn").length}`}
          onChange={(value) => {
            setPreference(value);
            if (!value.enabled) setFeedback(null);
            if (preference && preference.capture_generation !== value.capture_generation)
              void chat.refreshFeedback().catch(() => {});
          }}
        />
      </details>
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
                a model with a larger context below, or start a new conversation. Your draft is
                saved.
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
              compact
              models={chat.models}
              selectedId={chat.selectedId}
              onSelect={chat.setSelectedId}
              disabled={chat.active || pendingComparison}
            />
          )}
          {chat.canVaryAnswers && (
            <label>
              <input
                type="checkbox"
                checked={chat.variedAnswers}
                disabled={chat.active || pendingComparison}
                onChange={(event) => chat.setVariedAnswers(event.target.checked)}
              />
              Varied answers
              <small> Use sampling for new answers and their comparisons.</small>
            </label>
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
          {feedbackError && <p role="alert">{feedbackError}</p>}
          <MessageList
            onPromptSelect={(prompt) => {
              chat.setDraft(prompt);
              document.getElementById("chat-input")?.focus();
            }}
            messages={chat.messages}
            attachments={chat.attachments}
            feedback={(message) => {
              const row = feedback?.items?.find((item) => item.message_id === message.id);
              const turn = chat.turns.find((item) => item.assistant_message_id === message.id);
              return row &&
                cid &&
                message.text.trim() &&
                (!turn || turn.state === "completed") &&
                preference?.enabled ? (
                <FeedbackControls
                  conversationId={cid}
                  revision={chat.conversation!.revision}
                  row={row}
                  enabled={Boolean(preference?.enabled)}
                  complete={!turn || turn.state === "completed"}
                  pending={pendingComparison}
                  onChange={refreshFeedback}
                  onCreated={chat.recordComparison}
                />
              ) : null;
            }}
          />
          {cid &&
            chat.comparisons.map((receipt) => (
              <Comparison
                key={receipt.id}
                conversationId={cid}
                receipt={receipt}
                enabled={Boolean(preference?.enabled)}
                onChange={refreshFeedback}
              />
            ))}
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
              <div>
                <strong>{chat.canContinue ? "Response interrupted" : "No answer received"}</strong>
                <p>
                  {chat.canContinue
                    ? "The partial answer is saved above. You can continue it or try again."
                    : "The response ended before an answer was received. Try again."}
                </p>
              </div>
              <div className="chat-retry-actions">
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
                    aria-label="Continue from partial answer"
                  >
                    Continue answer
                  </button>
                )}
              </div>
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
          <div ref={scroll.end} className="chat-scroll-anchor" />
          {scroll.away && (
            <button className="chat-jump-latest" type="button" onClick={scroll.jump}>
              Jump to latest ↓
            </button>
          )}
          <div className="chat-composer-dock">
            <Composer
              onUpload={(file) => void chat.uploadFile(file)}
              uploadDisabled={
                !chat.selectedId ||
                chat.active ||
                chat.fileBusy ||
                Boolean(chat.conversation?.active_turn_id)
              }
              active={Boolean(chat.conversation?.active_turn_id)}
              stopping={chat.stopPending}
              onStop={() => void chat.stop()}
              files={
                chat.attachments.length > 0 ? (
                  <div className="composer-files">
                    <div className="composer-file-chips">
                      {chat.attachments
                        .filter(
                          (file) =>
                            chat.selections.some((s) => s.file_id === file.id) ||
                            file.state !== "ready",
                        )
                        .map((file) => (
                          <div className="composer-file-chip" key={file.id}>
                            <Icon name="paperclip" size={17} />
                            <span>
                              <strong>{file.filename}</strong>
                              <small>
                                {file.state === "ready"
                                  ? "Included in next message"
                                  : file.state === "processing"
                                    ? "Processing…"
                                    : file.state === "failed"
                                      ? "Processing failed"
                                      : file.state}
                              </small>
                            </span>
                            {chat.selections.some((s) => s.file_id === file.id) && (
                              <button
                                type="button"
                                aria-label={`Remove ${file.filename} from message`}
                                disabled={
                                  chat.active ||
                                  chat.fileBusy ||
                                  Boolean(chat.conversation?.active_turn_id)
                                }
                                onClick={() => chat.selectFile(null, file.id)}
                              >
                                <Icon name="close" size={14} />
                              </button>
                            )}
                          </div>
                        ))}
                    </div>
                    <details className="composer-file-manager">
                      <summary>Manage files ({chat.attachments.length})</summary>{" "}
                      <AttachmentList
                        embedded
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
                    </details>
                  </div>
                ) : chat.fileBusy ? (
                  <p className="composer-upload-status" role="status">
                    Uploading file…
                  </p>
                ) : null
              }
              value={chat.draft}
              onChange={chat.setDraft}
              onSend={() => void chat.send()}
              disabled={
                !chat.selectedId ||
                pendingComparison ||
                chat.active ||
                chat.fileBusy ||
                !chat.canSendSelections ||
                !chat.codeReady ||
                Boolean(chat.conversation?.active_turn_id)
              }
              status={chat.fileBusy ? "Preparing file…" : chat.status}
              mode={chat.mode}
            />
          </div>
        </div>
      </div>
    </main>
  );
}
