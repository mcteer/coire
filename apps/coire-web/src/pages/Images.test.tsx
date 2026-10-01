import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Images } from "./Images";

const JOB = "01J00000000000000000000000";

vi.mock("../hooks/useImageJob", () => ({
  useImageJob: () => ({
    observedJobId: JOB,
    state: "succeeded",
    step: 4,
    total: 4,
    terminal: true,
    error: null,
  }),
  isTerminalImageState: (state: string | null) =>
    state === "succeeded" || state === "failed" || state === "cancelled",
}));

vi.mock("../components/images/ImageForm", () => ({
  ImageForm: ({ onSubmit }: { onSubmit: (request: object) => void }) => (
    <button type="button" onClick={() => onSubmit({ model_id: "model", prompt: "test" })}>
      Generate test
    </button>
  ),
}));

vi.mock("../components/images/PresetEditor", () => ({
  PresetEditor: () => null,
}));

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("refreshes committed jobs and gallery once after a selected terminal event", async () => {
  const calls: Array<{ path: string; method: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      calls.push({ path, method });
      const body =
        method === "POST" && path === "/api/v1/images"
          ? { job_id: JOB, state: "queued" }
          : path === "/api/v1/images/models"
            ? { items: [{}] }
            : { items: [], next_cursor: null };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  render(<Images />);
  fireEvent.click(await screen.findByRole("button", { name: "Generate test" }));
  await waitFor(() =>
    expect(calls.filter(({ path }) => path.startsWith("/api/v1/images?"))).toHaveLength(3),
  );
  expect(
    calls.filter(({ path, method }) => path === "/api/v1/images" && method === "POST"),
  ).toHaveLength(1);
  expect(calls.filter(({ path }) => path.startsWith("/api/v1/image-outputs?"))).toHaveLength(3);
});

test("shows the durable job receipt and progress after submit", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const body =
        init?.method === "POST" && path === "/api/v1/images"
          ? { job_id: JOB, state: "queued" }
          : path === "/api/v1/images/models"
            ? { items: [{}] }
            : { items: [], next_cursor: null };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  render(<Images />);
  fireEvent.click(await screen.findByRole("button", { name: "Generate test" }));
  expect(await screen.findByText(/4 of 4/)).toBeInTheDocument();
  expect(screen.getByText(JOB)).toBeInTheDocument();
});

test("offers sign-in recovery on an expired image session", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));
  render(<Images />);
  expect(await screen.findByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/");
  expect(screen.getByRole("alert")).toBeInTheDocument();
});
