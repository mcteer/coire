import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";

afterEach(() => vi.restoreAllMocks());

const body = {
  status: "healthy",
  version: "0.1.0",
  services: [
    { name: "postgres", healthy: true, detail: null, checked_at: "", latency_ms: 12.3 },
    { name: "mcp", healthy: false, detail: "HTTP 503", checked_at: "", latency_ms: 4 },
  ],
  nodes: [],
  generated_at: "",
};

test("renders the aggregate status and every probed service", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ json: async () => body }));
  render(<App />);
  await waitFor(() => expect(screen.getByRole("heading", { name: "Coire" })).toBeInTheDocument());
  expect(screen.getAllByText("healthy").length).toBeGreaterThan(0);
  expect(screen.getByText("postgres")).toBeInTheDocument();
  expect(screen.getByText("HTTP 503")).toBeInTheDocument();
});

test("surfaces an unreachable control plane rather than rendering nothing", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("boom")));
  render(<App />);
  await waitFor(() => expect(screen.getByText(/cannot reach/)).toBeInTheDocument());
});
