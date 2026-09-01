import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ActivityPage } from "../../App";

afterEach(() => vi.restoreAllMocks());

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
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [item], next_cursor: null }), { status: 200 }),
    )
    .mockResolvedValueOnce(new Response(JSON.stringify(item), { status: 202 }))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 }),
    );
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
