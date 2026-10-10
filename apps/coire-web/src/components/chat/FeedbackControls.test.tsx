import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { FeedbackControls } from "./FeedbackControls";
import * as feedback from "../../api/feedback";

const row: feedback.MessageFeedback = { message_id: "message", eligibility: "eligible", tags: [] };
afterEach(() => vi.restoreAllMocks());
test("disabled capture prevents thumbs and comparison requests", () => {
  const vote = vi.spyOn(feedback, "updateThumb");
  render(
    <FeedbackControls
      conversationId="conversation"
      revision={3}
      row={row}
      enabled={false}
      complete
      onChange={vi.fn()}
      onCreated={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "Helpful answer" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Compare another answer" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Helpful answer" }));
  expect(vote).not.toHaveBeenCalled();
});
test("thumb submission is independent from pair creation", async () => {
  const vote = vi
    .spyOn(feedback, "updateThumb")
    .mockResolvedValue({
      id: "feedback",
      version: 1,
      judgement: "up",
      source: "owner",
      disclosure_version: "feedback-v1",
      disclosure: "Policy",
    });
  const compare = vi.spyOn(feedback, "createComparison");
  render(
    <FeedbackControls
      conversationId="conversation"
      revision={3}
      row={row}
      enabled
      complete
      onChange={vi.fn()}
      onCreated={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Helpful answer" }));
  await waitFor(() =>
    expect(vote).toHaveBeenCalledWith(
      "conversation",
      "message",
      expect.objectContaining({ expected_version: 0, judgement: "up", tags: [] }),
    ),
  );
  expect(compare).not.toHaveBeenCalled();
});
test("explicit compare uses the source identity and current conversation revision", async () => {
  const compare = vi
    .spyOn(feedback, "createComparison")
    .mockResolvedValue({
      id: "pair",
      version: 1,
      state: "queued",
      selection_state: "pending",
      events_path: "/events",
    });
  const created = vi.fn();
  render(
    <FeedbackControls
      conversationId="conversation"
      revision={3}
      row={row}
      enabled
      complete
      onChange={vi.fn()}
      onCreated={created}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Compare another answer" }));
  await waitFor(() =>
    expect(compare).toHaveBeenCalledWith(
      "conversation",
      expect.objectContaining({ expected_revision: 3, source_message_id: "message" }),
    ),
  );
  expect(created).toHaveBeenCalledWith(expect.objectContaining({ id: "pair" }));
});
