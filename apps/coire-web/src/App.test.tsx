import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";

afterEach(() => vi.restoreAllMocks());

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

test("hides the admin console from a non-admin", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ ...user, role: "user" }) }),
  );
  render(<App />);
  await waitFor(() => expect(screen.getByText("Admin access required")).toBeInTheDocument());
  expect(screen.queryByRole("navigation", { name: "Admin sections" })).not.toBeInTheDocument();
});

test("surfaces an authentication failure", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("boom")));
  render(<App />);
  await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());
});
