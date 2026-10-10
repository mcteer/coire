import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { FeedbackSettings } from "./FeedbackSettings";
import * as feedback from "../../api/feedback";
import { ApiError } from "../../api/client";

const preference: feedback.FeedbackPreference = {
  owner_id: "owner",
  enabled: true,
  capture_generation: 1,
  version: 1,
  changed_at: "2026-10-08T00:00:00Z",
  disclosure_version: "feedback-v1",
  disclosure:
    "Feedback may improve models on this platform. Unexported feedback is removed when capture is disabled. Published datasets and trained adapters remain unchanged.",
};
afterEach(() => vi.restoreAllMocks());

test("shows the withdrawal limit before enabling contribution controls", async () => {
  vi.spyOn(feedback, "getFeedbackPreference").mockResolvedValue(preference);
  const change = vi.fn();
  render(<FeedbackSettings onChange={change} />);
  expect(
    screen.getByText(/Already published training datasets and trained adapters remain unchanged/),
  ).toBeVisible();
  expect(await screen.findByRole("checkbox", { name: "Capture feedback" })).toBeChecked();
  expect(
    screen.getByText(/Published datasets and trained adapters remain unchanged/),
  ).toBeVisible();
  expect(change).toHaveBeenCalledWith(preference);
});

test("persists disable with current version and announced state", async () => {
  vi.spyOn(feedback, "getFeedbackPreference").mockResolvedValue(preference);
  const save = vi
    .spyOn(feedback, "updateFeedbackPreference")
    .mockResolvedValue({ ...preference, enabled: false, version: 2, capture_generation: 2 });
  const change = vi.fn();
  render(<FeedbackSettings onChange={change} />);
  fireEvent.click(await screen.findByRole("checkbox", { name: "Capture feedback" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: false,
        expected_version: 1,
        disclosure_version: "feedback-v1",
      }),
    ),
  );
  expect(await screen.findByText("Feedback capture is off.")).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "Capture feedback" })).not.toBeChecked();
});

test("refreshes a settings conflict without silently replaying the toggle", async () => {
  vi.spyOn(feedback, "getFeedbackPreference")
    .mockResolvedValueOnce(preference)
    .mockResolvedValue({ ...preference, version: 3 });
  const save = vi
    .spyOn(feedback, "updateFeedbackPreference")
    .mockRejectedValue(new ApiError(409, "Changed"));
  render(<FeedbackSettings onChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole("checkbox", { name: "Capture feedback" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Feedback settings changed");
  expect(save).toHaveBeenCalledTimes(1);
});

test("failed settings loading keeps contribution unavailable", async () => {
  vi.spyOn(feedback, "getFeedbackPreference").mockRejectedValue(new Error("Unavailable"));
  const change = vi.fn();
  render(<FeedbackSettings onChange={change} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Feedback settings are unavailable");
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(change).not.toHaveBeenCalled();
});
