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

test("opens owner image history while generation remains unavailable", async () => {
  location.hash = "#images";
  const fetchMock = vi.fn().mockImplementation((path: string) => {
    const body =
      path === "/api/v1/me"
        ? { ...user, role: "user" }
        : {
            items: [],
            next_cursor: null,
          };
    return Promise.resolve({ ok: true, status: 200, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  expect(await screen.findByRole("heading", { name: "Images" })).toBeInTheDocument();
  expect(await screen.findByText("No image jobs yet.")).toBeInTheDocument();
  expect(screen.getByText("No images yet.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Images" })).toHaveAttribute("aria-current", "page");
  expect(screen.queryByRole("button", { name: "Generate" })).not.toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringMatching(/^\/api\/v1\/images\?/),
    expect.anything(),
  );
});

test("submits a basic image job with one idempotency key", async () => {
  location.hash = "#images";
  const modelId = "00000000-0000-0000-0000-000000000002";
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000009");
  const fetchMock = vi.fn().mockImplementation((path: string, init?: RequestInit) => {
    const body =
      path === "/api/v1/me"
        ? { ...user, role: "user" }
        : path.startsWith("/api/v1/images/models")
          ? { items: [{ id: modelId, display_name: "Flux" }] }
          : path.startsWith("/api/v1/images/presets")
            ? { items: [] }
            : path === "/api/v1/images" && init?.method === "POST"
              ? { job_id: "01J00000000000000000000000", state: "queued" }
              : { items: [], next_cursor: null };
    return Promise.resolve({ ok: true, status: 200, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  fireEvent.change(await screen.findByRole("textbox", { name: "Image prompt" }), {
    target: { value: "a quiet portrait" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Generate" }));
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/images",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Idempotency-Key": "00000000-0000-4000-8000-000000000009",
        }),
      }),
    ),
  );
  const submit = fetchMock.mock.calls.find(([path]) => path === "/api/v1/images");
  expect(JSON.parse(String(submit?.[1]?.body))).toEqual(
    expect.objectContaining({
      schema_version: 1,
      model_id: modelId,
      prompt: "a quiet portrait",
      mode: "txt2img",
    }),
  );
});

test("stops an owner image job through the typed API", async () => {
  location.hash = "#images";
  const jobId = "01J00000000000000000000000";
  const job = { id: jobId, state: "running", effective_spec: {}, resolved: null };
  const fetchMock = vi.fn().mockImplementation((path: string, init: RequestInit) => {
    const body =
      path === "/api/v1/me"
        ? { ...user, role: "user" }
        : path.startsWith("/api/v1/images?")
          ? { items: [job], next_cursor: null }
          : path.startsWith("/api/v1/images/models") || path.startsWith("/api/v1/images/presets")
            ? { items: [] }
            : path.startsWith("/api/v1/image-outputs?")
              ? { items: [], next_cursor: null }
              : { ...job, state: "cancelling" };
    if (init?.method === "DELETE") expect(path).toBe(`/api/v1/images/${jobId}`);
    return Promise.resolve({ ok: true, status: 200, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: `Stop image job ${jobId}` }));
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/images/${jobId}`,
      expect.objectContaining({ method: "DELETE" }),
    ),
  );
});

test("offers reauthentication when image history access expires", async () => {
  location.hash = "#images";
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((path: string) =>
      Promise.resolve(
        path === "/api/v1/me"
          ? { ok: true, status: 200, json: async () => ({ ...user, role: "user" }) }
          : new Response(JSON.stringify({ title: "Unauthorized" }), {
              status: 401,
              headers: { "content-type": "application/problem+json" },
            }),
      ),
    ),
  );
  render(<App />);
  expect(await screen.findByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/");
});

test("requires two clicks before deleting an image output", async () => {
  location.hash = "#images";
  const outputId = "00000000-0000-0000-0000-000000000001";
  const fetchMock = vi.fn().mockImplementation((path: string, init: RequestInit) => {
    const body =
      path === "/api/v1/me"
        ? { ...user, role: "user" }
        : path.startsWith("/api/v1/images?")
          ? { items: [], next_cursor: null }
          : path.startsWith("/api/v1/images/models") || path.startsWith("/api/v1/images/presets")
            ? { items: [] }
            : path.startsWith("/api/v1/image-outputs?")
              ? {
                  items: [{ id: outputId, tag: "normal", created_at: "2026-09-30T20:00:00Z" }],
                  next_cursor: null,
                }
              : { output_id: outputId, state: "tombstoned" };
    if (init?.method === "DELETE") expect(path).toBe(`/api/v1/image-outputs/${outputId}`);
    return Promise.resolve({ ok: true, status: 200, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  const button = await screen.findByRole("button", { name: `Delete image ${outputId}` });
  fireEvent.click(button);
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  fireEvent.click(button);
  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true),
  );
  expect(await screen.findByText("No images yet.")).toBeInTheDocument();
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
