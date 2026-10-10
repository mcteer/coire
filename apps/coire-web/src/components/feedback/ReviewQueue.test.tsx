import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import * as api from "../../api/feedback";
import { ReviewQueue } from "./ReviewQueue";

const pair: api.FeedbackReviewDetail = {
  id: "01J00000000000000000000001",
  version: 1,
  state: "ready",
  selection_state: "pending",
  events_path: "/api/v1/chat/conversations/00000000-0000-4000-8000-000000000001/events",
  conversation_id: "00000000-0000-4000-8000-000000000001",
  source_message_id: "00000000-0000-4000-8000-000000000002",
  owner_id: "00000000-0000-4000-8000-000000000003",
  target: null,
  original: "original answer",
  candidate: "candidate answer",
  eligibility: "eligible",
  expires_at: "2026-10-10T00:00:00Z",
  created_at: "2026-10-09T00:00:00Z",
  prompt: [{ role: "user", content: "question" }],
  disclosure_version: "feedback-v1",
  disclosure: "Published datasets remain",
};
afterEach(() => vi.restoreAllMocks());
function reads() {
  vi.spyOn(api, "listFeedbackReview").mockResolvedValue({ items: [pair] });
  return vi.spyOn(api, "getFeedbackReview").mockResolvedValue(pair);
}

test("keyboard skip is a separate reviewer action and skipped pairs remain revisitable", async () => {
  reads();
  const judge = vi
    .spyOn(api, "judgeFeedbackPair")
    .mockResolvedValue({ comparison_id: pair.id, skipped: true });
  render(<ReviewQueue />);
  const skip = await screen.findByRole("button", { name: "Skip for now" });
  skip.focus();
  expect(skip).toHaveFocus();
  fireEvent.click(skip, { detail: 0 });
  await waitFor(() =>
    expect(judge).toHaveBeenCalledWith(
      pair.id,
      { expected_version: 0, choice: "skip", tags: [] },
      expect.any(String),
    ),
  );
  expect(await screen.findByRole("status")).toHaveTextContent("Skipped for you");
  fireEvent.change(screen.getByLabelText("Review queue"), { target: { value: "skipped" } });
  await waitFor(() => expect(api.listFeedbackReview).toHaveBeenCalledWith("skipped"));
});

test("stale judgement refreshes its version without automatically resubmitting", async () => {
  const get = reads();
  const judge = vi.spyOn(api, "judgeFeedbackPair").mockRejectedValue(new Error("Conflict"));
  render(<ReviewQueue />);
  const choose = await screen.findByRole("button", { name: "Prefer candidate" });
  get.mockResolvedValue({
    ...pair,
    admin_judgement: {
      id: "00000000-0000-4000-8000-000000000004",
      version: 2,
      source: "admin",
      judgement: "original",
      disclosure_version: "feedback-v1",
      disclosure: "Published datasets remain",
    },
  });
  fireEvent.click(choose);
  expect(await screen.findByRole("alert")).toHaveTextContent("Decision was not saved");
  await screen.findByText(/Admin choice: original · version 2/);
  expect(judge).toHaveBeenCalledTimes(1);
});

test("withdrawal during a decision removes copied answers and disables judgement", async () => {
  const get = reads();
  vi.spyOn(api, "judgeFeedbackPair").mockRejectedValue(new Error("Withdrawn"));
  render(<ReviewQueue />);
  const choose = await screen.findByRole("button", { name: "Prefer original" });
  get.mockRejectedValue(new Error("Withdrawn"));
  fireEvent.click(choose);
  await waitFor(() => expect(screen.queryByText("original answer")).not.toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Prefer candidate" })).not.toBeInTheDocument();
});

test("a failed transport keeps the same decision identity when retrying unchanged input", async () => {
  reads();
  const judge = vi
    .spyOn(api, "judgeFeedbackPair")
    .mockRejectedValue(new Error("Transport unavailable"));
  render(<ReviewQueue />);
  fireEvent.click(await screen.findByRole("button", { name: "Prefer original" }));
  await screen.findByRole("alert");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Prefer original" })).not.toBeDisabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Prefer original" }));
  await waitFor(() => expect(judge).toHaveBeenCalledTimes(2));
  expect(judge.mock.calls[0][2]).toBe(judge.mock.calls[1][2]);
});
