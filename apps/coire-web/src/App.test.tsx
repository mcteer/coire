import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";
import { loadChatDrafts, saveChatDrafts } from "./api/chatDrafts";

afterEach(() => {
  vi.restoreAllMocks();
  location.hash = "";
  sessionStorage.clear();
});

const user = {
  id: "00000000-0000-0000-0000-000000000001",
  email: "admin@example.test",
  display_name: "Admin",
  role: "admin",
  active: true,
  entitlements: [],
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

test("opens Chat for a non-admin without an admin navigation link", async () => {
  location.hash = "";
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ...user, role: "user" }),
      })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ data: [] }) })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ data: [], next_cursor: null }),
      }),
  );
  render(<App />);
  await waitFor(() => expect(screen.getByRole("heading", { name: "Chat" })).toBeInTheDocument());
  expect(await screen.findByText(/No chat models are available/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  expect(screen.queryByRole("navigation", { name: "Admin sections" })).not.toBeInTheDocument();
});

test("signs out through same-origin Access after clearing the verified owner's draft", async () => {
  loadChatDrafts(user.id);
  saveChatDrafts(user.id, new Map([["new", { text: "Private draft", modelId: null }]]));
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ...user, role: "user" }),
      })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ data: [] }) })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ data: [], next_cursor: null }),
      }),
  );
  render(<App />);
  const link = await screen.findByRole("link", { name: "Sign out" });
  expect(link).toHaveAttribute("href", "/cdn-cgi/access/logout");
  link.addEventListener("click", (event) => event.preventDefault());
  fireEvent.click(link);
  expect(sessionStorage.getItem("coire.chat.drafts." + user.id)).toBeNull();
  expect(sessionStorage.getItem("coire.chat.draft-owner")).toBeNull();
});

test("refuses an ordinary user who requests an admin route", async () => {
  location.hash = "#admin/overview";
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ...user, role: "user" }),
    }),
  );
  render(<App />);
  await waitFor(() => expect(screen.getByText("Admin access required")).toBeInTheDocument());
});

test("surfaces an authentication failure", async () => {
  location.hash = "";
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("boom")));
  render(<App />);
  await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());
});

test("offers same-origin reauthentication when the edge session expires", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ title: "Unauthorized" }), {
        status: 401,
        headers: { "content-type": "application/problem+json" },
      }),
    ),
  );
  render(<App />);
  expect(await screen.findByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/");
});
