import type { ReactNode } from "react";
import type { ConsoleSnapshot } from "../api/client";

export type AdminTab = "overview" | "models" | "instances" | "activity" | "identity" | "audit";
const tabs: [AdminTab, string][] = [
  ["overview", "Overview"],
  ["models", "Models"],
  ["instances", "Instances"],
  ["activity", "Runs & jobs"],
  ["identity", "Users & keys"],
  ["audit", "Audit"],
];

export function AppShell({
  view,
  canAdmin = false,
  tab,
  setTab,
  snapshot,
  onSignOut,
  children,
}: {
  view: "chat" | "images" | "admin";
  canAdmin?: boolean;
  tab?: AdminTab;
  setTab?: (tab: AdminTab) => void;
  snapshot?: ConsoleSnapshot | null;
  onSignOut?: () => void;
  children: ReactNode;
}) {
  const health = snapshot?.cluster.nodes.some((node) => node.reachability !== "healthy")
    ? "degraded"
    : "healthy";
  return (
    <div className={view === "chat" ? "app app-chat" : "app"}>
      <header className="header">
        <div className="brand">
          <span className="logo">C</span>
          <b>Coire</b>
          <span className="muted">
            /{" "}
            {view === "chat"
              ? "Chat"
              : view === "images"
                ? "Images"
                : "Admin / " + tabs.find(([id]) => id === tab)?.[1]}
          </span>
        </div>
        <div className="chips">
          {view === "admin" && (
            <>
              <span className="chip">
                <i className={"dot " + health} />
                {health}
              </span>
              <span className="chip mono">
                {snapshot ? new Date(snapshot.observed_at).toLocaleTimeString() : "connecting"}
              </span>
            </>
          )}
          {onSignOut && (
            <a className="chip" href="/cdn-cgi/access/logout" onClick={onSignOut}>
              Sign out
            </a>
          )}
        </div>
      </header>
      {view === "admin" && tab && setTab && (
        <nav className="tabs glass" aria-label="Admin sections">
          {tabs.map(([id, label]) => (
            <button
              className={"tab " + (tab === id ? "active" : "")}
              aria-current={tab === id ? "page" : undefined}
              onClick={() => setTab(id)}
              key={id}
            >
              {label}
            </button>
          ))}
        </nav>
      )}
      {children}
      <nav className="dock glass" aria-label="Primary">
        <a
          className={view === "chat" ? "active" : ""}
          aria-current={view === "chat" ? "page" : undefined}
          href="#chat"
        >
          Chat
        </a>
        <a
          className={view === "images" ? "active" : ""}
          aria-current={view === "images" ? "page" : undefined}
          href="#images"
        >
          Images
        </a>
        {canAdmin && (
          <a
            className={view === "admin" ? "active" : ""}
            aria-current={view === "admin" ? "page" : undefined}
            href="#admin/overview"
          >
            Admin
          </a>
        )}
      </nav>
    </div>
  );
}
