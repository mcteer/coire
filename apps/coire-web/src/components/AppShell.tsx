import { useEffect, useRef, useState, type ReactNode } from "react";
import type { ConsoleSnapshot } from "../api/client";
import { useTheme } from "../hooks/useTheme";
import { CoireMark, Icon, type IconName } from "./Icons";

export type AdminTab =
  "overview" | "models" | "instances" | "activity" | "identity" | "audit" | "upgrades";
const tabs: [AdminTab, string][] = [
  ["overview", "Overview"],
  ["models", "Models"],
  ["instances", "Instances"],
  ["activity", "Runs & jobs"],
  ["identity", "Users & keys"],
  ["audit", "Audit"],
  ["upgrades", "Upgrades"],
];
const titles = { chat: "Chat", images: "Images", training: "Training", admin: "Administration" };

export function AppShell({
  view,
  canAdmin = false,
  tab,
  setTab,
  snapshot,
  onSignOut,
  children,
}: {
  view: "chat" | "images" | "training" | "admin";
  canAdmin?: boolean;
  tab?: AdminTab;
  setTab?: (tab: AdminTab) => void;
  snapshot?: ConsoleSnapshot | null;
  onSignOut?: () => void;
  children: ReactNode;
}) {
  const { theme, toggleTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLButtonElement>(null);
  const sidebar = useRef<HTMLElement>(null);
  const close = () => {
    setOpen(false);
    menu.current?.focus();
  };
  const dismissNavigation = () => {
    if (open) close();
  };
  useEffect(() => {
    if (open) sidebar.current?.querySelector<HTMLAnchorElement>("a[href]")?.focus();
  }, [open]);
  const healthy = Boolean(
    snapshot &&
    snapshot.core?.health === "healthy" &&
    (snapshot.alerts?.length ?? 0) === 0 &&
    snapshot.cluster.nodes.length &&
    snapshot.cluster.nodes.every((node) => node.reachability === "healthy" && !node.stale),
  );
  const status = !snapshot ? "Connecting" : healthy ? "All systems healthy" : "Needs attention";
  const primary: { id: typeof view; label: string; icon: IconName; href: string }[] = [
    { id: "chat", label: "Chat", icon: "chat", href: "/#chat" },
    { id: "images", label: "Images", icon: "images", href: "/#images" },
    ...(canAdmin
      ? [
          {
            id: "training" as const,
            label: "Training",
            icon: "training" as const,
            href: "/#training",
          },
          {
            id: "admin" as const,
            label: "Admin",
            icon: "admin" as const,
            href: "/#admin/overview",
          },
        ]
      : []),
  ];
  return (
    <div className={`app workspace app-${view}${open ? " sidebar-open" : ""}`}>
      <a className="skip-link" href="#workspace-content">
        Skip to content
      </a>
      {open && <button className="sidebar-scrim" aria-label="Close navigation" onClick={close} />}
      <aside
        ref={sidebar}
        id="workspace-navigation"
        className="workspace-sidebar"
        aria-label="Workspace navigation"
        onKeyDown={(event) => {
          if (!open) return;
          if (event.key === "Escape") {
            event.preventDefault();
            close();
          }
          if (event.key === "Tab") {
            const items = [
              ...(sidebar.current?.querySelectorAll<HTMLElement>(
                "a[href],button:not([disabled])",
              ) ?? []),
            ];
            const first = items[0],
              last = items[items.length - 1];
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last?.focus();
            }
            if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <a
          className="workspace-brand"
          href="/#chat"
          onClick={dismissNavigation}
          aria-label="Coire home"
        >
          <span className="brand-mark">
            <CoireMark />
          </span>
          <span>
            Coire<small>YOUR AI WORKSPACE</small>
          </span>
        </a>
        <div className="nav-section-label">Workspace</div>
        <nav className="workspace-primary" aria-label="Primary">
          {primary.map((item) => (
            <a
              key={item.id}
              href={item.href}
              className={view === item.id ? "active" : ""}
              aria-current={view === item.id ? "page" : undefined}
              onClick={dismissNavigation}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
              {view === item.id && <span className="nav-active-indicator" aria-hidden="true" />}
            </a>
          ))}
        </nav>
        {view === "admin" && tab && setTab && (
          <div className="workspace-admin-nav">
            <div className="nav-section-label">Administration</div>
            <nav aria-label="Admin sections">
              {tabs
                .filter(([id]) => id !== "upgrades" || snapshot?.capabilities.upgrades)
                .map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    className={tab === id ? "active" : ""}
                    aria-current={tab === id ? "page" : undefined}
                    onClick={() => {
                      setTab(id);
                      dismissNavigation();
                    }}
                  >
                    <Icon name={id} />
                    <span>{label}</span>
                  </button>
                ))}
            </nav>
          </div>
        )}
        <div className="sidebar-footer">
          <span className="sidebar-workspace-icon">
            <CoireMark size={22} />
          </span>
          <span>
            Private workspace<small>Powered by your infrastructure</small>
          </span>
        </div>
      </aside>
      <div className="workspace-body">
        <header className="workspace-header">
          <div className="workspace-breadcrumb">
            <button
              ref={menu}
              className="icon-button mobile-menu"
              type="button"
              aria-label={open ? "Close workspace menu" : "Open workspace menu"}
              aria-expanded={open}
              aria-controls="workspace-navigation"
              onClick={() => (open ? close() : setOpen(true))}
            >
              <Icon name={open ? "close" : "menu"} />
            </button>
            <span className="breadcrumb-parent">Workspace</span>
            <Icon name="chevron" size={13} />
            <span>
              {view === "admin"
                ? (tabs.find(([id]) => id === tab)?.[1] ?? titles[view])
                : titles[view]}
            </span>
          </div>
          <div className="workspace-header-actions">
            {view === "admin" && (
              <span
                className={`workspace-status ${healthy ? "healthy" : "unknown"}`}
                aria-label="Cluster status"
              >
                <i className={`dot ${healthy ? "healthy" : "unknown"}`} />
                {status}
              </span>
            )}
            <button
              className="icon-button theme-toggle"
              type="button"
              aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}
              onClick={toggleTheme}
            >
              <Icon name={theme === "light" ? "moon" : "sun"} />
            </button>
            {onSignOut && (
              <button
                aria-label="Sign out"
                className="signout-button"
                type="button"
                onClick={onSignOut}
              >
                <Icon name="logout" size={16} />
                <span>Sign out</span>
              </button>
            )}
          </div>
        </header>
        <div id="workspace-content" className="workspace-content" tabIndex={-1}>
          {children}
        </div>
      </div>
    </div>
  );
}
