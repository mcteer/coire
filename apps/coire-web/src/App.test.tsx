import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";

afterEach(() => {
  vi.restoreAllMocks();
  location.hash = "";
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
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ data: [] }) }),
  );
  render(<App />);
  await waitFor(() => expect(screen.getByRole("heading", { name: "Chat" })).toBeInTheDocument());
  expect(await screen.findByText(/No chat models are available/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  expect(screen.queryByRole("navigation", { name: "Admin sections" })).not.toBeInTheDocument();
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
