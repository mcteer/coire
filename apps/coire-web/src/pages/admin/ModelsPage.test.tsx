import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { DataPage } from "../../App";

afterEach(() => vi.restoreAllMocks());

test("renders lifecycle controls and requires named confirmation for retire", async () => {
  const model = {
    id: "00000000-0000-4000-8000-000000000001",
    display_name: "Tiny",
    slug: "tiny",
    state: "ready",
    visibility: "admin_only",
    placement_policy: "single:auto",
    updated_at: "2026-09-01T00:00:00Z",
    copies: [],
    engines: [],
  };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify([model]), { status: 200 }))
    .mockResolvedValueOnce(new Response(null, { status: 202 }))
    .mockResolvedValueOnce(new Response(JSON.stringify([model]), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  render(<DataPage kind="models" />);
  const retire = await screen.findByRole("button", { name: "Retire Tiny" });
  fireEvent.click(retire);
  expect(screen.getByRole("button", { name: "Retire Tiny" })).toHaveTextContent(
    "Confirm retire Tiny?",
  );
  expect(fetchMock).toHaveBeenCalledTimes(1);
  fireEvent.click(retire);
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/admin/models/${model.id}/retire`,
      expect.objectContaining({ method: "POST" }),
    ),
  );
});
