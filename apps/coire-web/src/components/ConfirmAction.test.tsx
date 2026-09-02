import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { ConfirmAction } from "./ConfirmAction";

test("names the destructive target and requires a second click", () => {
  const action = vi.fn().mockResolvedValue(undefined);
  render(<ConfirmAction target="Qwen Coder" label="Retire" onConfirm={action} />);
  fireEvent.click(screen.getByRole("button", { name: "Retire Qwen Coder" }));
  expect(action).not.toHaveBeenCalled();
  expect(screen.getByText("Confirm retire Qwen Coder?")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retire Qwen Coder" }));
  expect(action).toHaveBeenCalledOnce();
});
