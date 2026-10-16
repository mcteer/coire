import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { MessageList } from "./MessageList";

test("suggestions provide an explicit draft without submitting or changing model", () => {
  const choose = vi.fn();
  render(<MessageList messages={[]} attachments={[]} onPromptSelect={choose} />);
  expect(
    screen.getByRole("heading", { name: "What would you like to explore?" }),
  ).toBeInTheDocument();
  expect(choose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Make a plan" }));
  expect(choose).toHaveBeenCalledExactlyOnceWith("Help me turn an idea into an actionable plan.");
});
