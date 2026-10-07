import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ActivityPage } from "../../App";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("lists shipped activity, omits agent controls, and confirms stop", async () => {
  const item = {
    id: "00000000-0000-4000-8000-000000000001",
    kind: "instance",
    owner: "platform",
    target: "Tiny",
    state: "ready",
    started_at: "2026-09-01T00:00:00Z",
    elapsed_seconds: 5,
    progress_percent: null,
    failure_reason: null,
    can_stop: true,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/v1/admin/console/training-activity?limit=25")
      return new Response(JSON.stringify({ items: [], next_cursor: null }));
    if (path === `/api/v1/instances/${item.id}` && init?.method === "DELETE") {
      return new Response(JSON.stringify(item), { status: 202 });
    }
    if (path === "/api/v1/admin/image-jobs?limit=50") {
      return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 });
    }
    return new Response(JSON.stringify({ items: [item], next_cursor: null }), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<ActivityPage />);
  const stop = await screen.findByRole("button", { name: "Stop 00000000" });
  expect(screen.queryByText(/agent run/i)).not.toBeInTheDocument();
  fireEvent.click(stop);
  fireEvent.click(stop);
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/instances/${item.id}`,
      expect.objectContaining({ method: "DELETE" }),
    ),
  );
});

test("shows admin image jobs and confirms a fenced kill", async () => {
  const job = {
    job_id: "01J00000000000000000000000",
    owner_id: "00000000-0000-4000-8000-000000000002",
    model_id: "00000000-0000-4000-8000-000000000003",
    state: "running",
    started_at: "2026-10-01T00:00:00Z",
    elapsed_seconds: 5,
    progress_step: null,
    progress_total: 4,
    safe_failure_code: null,
    can_stop: true,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/v1/admin/console/training-activity?limit=25")
      return new Response(JSON.stringify({ items: [], next_cursor: null }));
    if (path === `/api/v1/admin/image-jobs/${job.job_id}` && init?.method === "DELETE") {
      return new Response(JSON.stringify({ ...job, state: "cancelling" }), { status: 202 });
    }
    if (path === "/api/v1/admin/image-jobs?limit=50") {
      return new Response(JSON.stringify({ items: [job], next_cursor: null }), { status: 200 });
    }
    return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<ActivityPage />);
  const kill = await screen.findByRole("button", { name: `Kill image job ${job.job_id}` });
  fireEvent.click(kill);
  fireEvent.click(kill);
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/admin/image-jobs/${job.job_id}`,
      expect.objectContaining({ method: "DELETE" }),
    ),
  );
});

test("offers worker unload on image instances through the audited admin route", async () => {
  const instance = {
    id: "00000000-0000-4000-8000-000000000004",
    kind: "image_worker",
    owner: "platform",
    target: "Image base",
    state: "ready",
    started_at: "2026-10-01T00:00:00Z",
    elapsed_seconds: 5,
    progress_percent: null,
    failure_reason: null,
    can_stop: true,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/v1/admin/console/training-activity?limit=25")
      return new Response(JSON.stringify({ items: [], next_cursor: null }));
    if (path === `/api/v1/admin/image-workers/${instance.id}` && init?.method === "DELETE") {
      return new Response(JSON.stringify({ instance_id: instance.id, state: "failed" }), {
        status: 200,
      });
    }
    if (path === "/api/v1/admin/image-jobs?limit=50") {
      return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 });
    }
    return new Response(JSON.stringify({ items: [instance], next_cursor: null }), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<ActivityPage />);
  const unload = await screen.findByRole("button", {
    name: `Unload image worker ${instance.id}`,
  });
  fireEvent.click(unload);
  fireEvent.click(unload);
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/admin/image-workers/${instance.id}`,
      expect.objectContaining({ method: "DELETE" }),
    ),
  );
});
