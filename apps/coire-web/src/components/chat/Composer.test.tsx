import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { Composer } from "./Composer";

test("keeps the draft through cold, queued, failure and retry-ready states", () => {
  const onChange = vi.fn();
  const onSend = vi.fn();
  const props = { value: "Explain this", onChange, onSend };
  const view = render(
    <Composer {...props} disabled status="Warming up the model · estimate unavailable" />,
  );
  const input = screen.getByRole("textbox", { name: "Message" });
  expect(input).toHaveValue("Explain this");
  expect(screen.getByRole("status")).toHaveTextContent("estimate unavailable");
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

  view.rerender(<Composer {...props} disabled status="Waiting for capacity…" />);
  expect(screen.getByRole("status")).toHaveTextContent("Waiting for capacity");
  expect(input).toHaveValue("Explain this");

  view.rerender(<Composer {...props} disabled status="Generation failed; retry this turn" />);
  expect(screen.getByRole("status")).toHaveTextContent("Generation failed");
  expect(onSend).not.toHaveBeenCalled();

  view.rerender(<Composer {...props} disabled={false} status={null} />);
  fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
  expect(onSend).toHaveBeenCalledTimes(1);
});

test("Shift+Enter remains available for multiline drafts", () => {
  const onSend = vi.fn();
  render(
    <Composer value="Line one" onChange={vi.fn()} onSend={onSend} disabled={false} status={null} />,
  );
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), {
    key: "Enter",
    code: "Enter",
    shiftKey: true,
  });
  expect(onSend).not.toHaveBeenCalled();
});
