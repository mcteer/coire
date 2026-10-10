import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Comparison } from "./Comparison";
import * as feedback from "../../api/feedback";

const receipt: feedback.ComparisonReceipt = {
  id: "pair",
  version: 3,
  state: "ready",
  selection_state: "pending",
  events_path: "/events",
};
const detail: feedback.ComparisonDetail = {
  ...receipt,
  conversation_id: "conversation",
  source_message_id: "message",
  target: null,
  original: "Original answer text",
  candidate: "Alternative answer text",
  eligibility: "eligible",
  created_at: "2026-10-08T00:00:00Z",
  expires_at: "2026-10-09T00:00:00Z",
  disclosure_version: "feedback-v1",
  disclosure: "Policy",
};
afterEach(() => vi.restoreAllMocks());
test("keeps the original active until an explicit versioned choice", async () => {
  vi.spyOn(feedback, "getComparison").mockResolvedValue(detail);
  const select = vi.spyOn(feedback, "selectComparison").mockResolvedValue({
    comparison: {
      ...receipt,
      version: 4,
      selection_state: "chosen",
    },
    conversation_revision: 4,
    feedback: {
      id: "feedback",
      version: 1,
      judgement: "candidate",
      source: "owner",
      disclosure_version: "feedback-v1",
      disclosure: "Policy",
    },
  });
  render(<Comparison conversationId="conversation" receipt={receipt} enabled onChange={vi.fn()} />);
  expect(await screen.findByText("Alternative answer text")).toBeVisible();
  expect(screen.getByText(/Your original answer stays active until you choose/)).toBeVisible();
  expect(select).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("textbox", { name: "Comparison tags" }), {
    target: { value: "clear" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Choose alternative answer" }));
  await waitFor(() =>
    expect(select).toHaveBeenCalledWith(
      "conversation",
      "pair",
      expect.objectContaining({ expected_version: 3, candidate: "candidate", tags: ["clear"] }),
    ),
  );
});
test("capture disable immediately removes copied comparison bodies", async () => {
  vi.spyOn(feedback, "getComparison").mockResolvedValue(detail);
  const view = render(
    <Comparison conversationId="conversation" receipt={receipt} enabled onChange={vi.fn()} />,
  );
  expect(await screen.findByText("Alternative answer text")).toBeVisible();
  view.rerender(
    <Comparison
      conversationId="conversation"
      receipt={receipt}
      enabled={false}
      onChange={vi.fn()}
    />,
  );
  expect(screen.queryByText("Alternative answer text")).toBeNull();
  expect(screen.queryByRole("button", { name: "Choose alternative answer" })).toBeNull();
});
test("later feedback edits explain that the active answer stays fixed", async () => {
  vi.spyOn(feedback, "getComparison").mockResolvedValue({
    ...detail,
    selection_state: "chosen",
    selected: "candidate",
  });
  render(
    <Comparison
      conversationId="conversation"
      receipt={{ ...receipt, selection_state: "chosen" }}
      enabled
      onChange={vi.fn()}
    />,
  );
  expect(
    await screen.findByText(/Changing your feedback does not change the answer/),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Prefer original answer" })).toBeVisible();
});
