import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { AppShell } from "./AppShell";
import { consoleSnapshot } from "../test/fixtures";

const originalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");
beforeEach(() => {
  // Node 26 may expose an undefined native localStorage over jsdom's storage getter.
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      get length() {
        return values.size;
      },
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => {
        values.set(key, value);
      },
      removeItem: (key: string) => {
        values.delete(key);
      },
      clear: () => values.clear(),
      key: (index: number) => [...values.keys()][index] ?? null,
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  if (originalStorage) Object.defineProperty(window, "localStorage", originalStorage);
  else Reflect.deleteProperty(window, "localStorage");
  delete document.documentElement.dataset.theme;
  vi.restoreAllMocks();
});

test("keeps primary routes and admin sections role-aware", () => {
  const { rerender } = render(
    <AppShell view="chat">
      <main>Content</main>
    </AppShell>,
  );
  expect(screen.getByRole("link", { name: "Chat" })).toHaveAttribute("aria-current", "page");
  expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Training" })).not.toBeInTheDocument();
  const setTab = vi.fn();
  rerender(
    <AppShell view="admin" canAdmin tab="models" setTab={setTab} snapshot={consoleSnapshot()}>
      <main>Content</main>
    </AppShell>,
  );
  expect(screen.getByRole("navigation", { name: "Admin sections" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Models" })).toHaveAttribute("aria-current", "page");
  fireEvent.click(screen.getByRole("button", { name: "Users & keys" }));
  expect(setTab).toHaveBeenCalledWith("identity");
  expect(screen.queryByRole("button", { name: "Upgrades" })).not.toBeInTheDocument();
});

test("never reports an unknown cluster as healthy", () => {
  render(
    <AppShell view="admin" canAdmin>
      <main>Content</main>
    </AppShell>,
  );
  expect(screen.getByLabelText("Cluster status")).toHaveTextContent("Connecting");
  expect(screen.queryByText("All systems healthy")).not.toBeInTheDocument();
});

test("theme preference persists across remount without touching private drafts", async () => {
  window.localStorage.setItem("unrelated-preference", "keep");
  sessionStorage.setItem("coire.chat.test", "private draft");
  const { unmount } = render(
    <AppShell view="chat">
      <main>Content</main>
    </AppShell>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Switch to dark theme" }));
  await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
  expect(window.localStorage.getItem("coire.theme")).toBe("dark");
  unmount();
  render(
    <AppShell view="chat">
      <main>Content</main>
    </AppShell>,
  );
  expect(screen.getByRole("button", { name: "Switch to light theme" })).toBeInTheDocument();
  expect(sessionStorage.getItem("coire.chat.test")).toBe("private draft");
  expect(window.localStorage.getItem("unrelated-preference")).toBe("keep");
});

test("blocked theme storage does not prevent navigation or theme changes", () => {
  vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  render(
    <AppShell view="images">
      <main>Content</main>
    </AppShell>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Switch to dark theme" }));
  expect(screen.getByRole("button", { name: "Switch to light theme" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Images" })).toHaveAttribute("aria-current", "page");
});

test("mobile navigation opens with focus and Escape restores its toggle", async () => {
  render(
    <AppShell view="chat">
      <main>Content</main>
    </AppShell>,
  );
  const toggle = screen.getByRole("button", { name: "Open workspace menu" });
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  await waitFor(() => expect(screen.getByRole("link", { name: "Coire home" })).toHaveFocus());
  fireEvent.keyDown(screen.getByLabelText("Workspace navigation"), { key: "Escape" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(toggle).toHaveFocus();
});

test("active alerts prevent a healthy banner even when every host is reachable", () => {
  const at = "2026-09-01T00:00:00Z";
  const snapshot = consoleSnapshot({
    core: {
      host_name: "core",
      source: "core-control-plane-runtime",
      health: "healthy",
      memory_total_bytes: 100,
      memory_free_bytes: 50,
      disk_total_bytes: 100,
      disk_free_bytes: 50,
      observed_at: at,
    },
    cluster: {
      observed_at: at,
      instances: [],
      nodes: [
        {
          id: "00000000-0000-4000-8000-000000000001",
          name: "coire-edge-a",
          reachability: "healthy",
          thermal_state: "nominal",
          health_observed_at: at,
          stale: false,
          memory_total_bytes: 100,
          memory_free_bytes: 50,
          disk_total_bytes: 100,
          disk_free_bytes: 50,
          budget_bytes: 90,
          reserved_bytes: 0,
          reservations: [],
        },
      ],
    },
  });
  const { rerender } = render(
    <AppShell view="admin" canAdmin snapshot={snapshot}>
      <main>Content</main>
    </AppShell>,
  );
  expect(screen.getByLabelText("Cluster status")).toHaveTextContent("All systems healthy");
  rerender(
    <AppShell
      view="admin"
      canAdmin
      snapshot={{
        ...snapshot,
        alerts: [
          {
            severity: "warning",
            title: "Ledger drift",
            detail: "Measured memory differs from the ledger",
          },
        ],
      }}
    >
      <main>Content</main>
    </AppShell>,
  );
  expect(screen.getByLabelText("Cluster status")).toHaveTextContent("Needs attention");
  expect(screen.queryByText("All systems healthy")).not.toBeInTheDocument();
});
