import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ExportHistory } from "./ExportHistory";
import * as feedback from "../../api/feedback";
import * as training from "../../api/training";
import { trainingDataset } from "../../test/trainingFixtures";

const record: feedback.PreferenceExportDetail = {
  id: "export",
  state: "succeeded",
  version: 3,
  owner_id: "admin",
  request: {
    name: "Explicit pairs",
    license_note: "Local consent",
    model_id: "model",
    variant_id: "variant",
    source: "owner_preferred",
    split_seed: 0,
    validation_fraction: 0.05,
  },
  selected_count: 2,
  matched_count: 2,
  excluded_count: 0,
  pair_limit: 10000,
  byte_limit: 268435456,
  cleanup_pending: false,
  dataset_id: trainingDataset.id,
  warnings: ["small_sample"],
  created_at: "2026-10-08T00:00:00Z",
  deadline: "2026-10-08T00:05:00Z",
};
afterEach(() => vi.restoreAllMocks());

test("publication remains separate from analyzed dataset readiness", async () => {
  vi.spyOn(feedback, "listFeedbackExports").mockResolvedValue({ items: [record] });
  vi.spyOn(training, "getDataset").mockResolvedValue({ ...trainingDataset, state: "analyzing" });
  render(<ExportHistory refreshKey={0} />);
  expect(await screen.findByText(/Training readiness: analyzing/)).toBeVisible();
  expect(screen.getByText(/Small sample: fewer than 20/)).toBeVisible();
  expect(screen.getByRole("link", { name: "View private dataset" })).toHaveAttribute(
    "href",
    "#training",
  );
});

test("failed exports retain counted cleanup status and explicit oversize count", async () => {
  vi.spyOn(feedback, "listFeedbackExports").mockResolvedValue({
    items: [
      {
        ...record,
        state: "failed",
        reason: "oversize",
        dataset_id: null,
        matched_count: 10001,
        selected_count: 0,
        cleanup_pending: true,
      },
    ],
  });
  render(<ExportHistory refreshKey={0} />);
  expect(await screen.findByText(/10001 matches/)).toBeVisible();
  expect(screen.getByText(/storage remains reserved/)).toBeVisible();
  expect(screen.queryByRole("link", { name: "View private dataset" })).toBeNull();
});

test("refresh preserves explicitly loaded older exports", async () => {
  const list = vi
    .spyOn(feedback, "listFeedbackExports")
    .mockImplementation(async (cursor) =>
      cursor
        ? {
            items: [
              { ...record, id: "older", request: { ...record.request, name: "Older pairs" } },
            ],
          }
        : { items: [record], next_cursor: "older-page" },
    );
  vi.spyOn(training, "getDataset").mockResolvedValue(trainingDataset);
  const view = render(<ExportHistory refreshKey={0} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load older exports" }));
  expect(await screen.findByRole("heading", { name: /Older pairs/ })).toBeVisible();
  await act(async () => {
    view.rerender(<ExportHistory refreshKey={1} />);
  });
  await waitFor(() => expect(list).toHaveBeenCalledTimes(3));
  expect(screen.getByRole("heading", { name: /Older pairs/ })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Load older exports" })).toBeNull();
});
