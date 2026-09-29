import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { ReasoningBlock } from "./ReasoningBlock";

test("keeps reasoning collapsed and escapes executable markup", () => {
  const view = render(<ReasoningBlock text="private <script>alert(1)</script>" />);
  expect(screen.getByText("Reasoning")).toBeInTheDocument();
  expect(view.container.querySelector("details")).not.toHaveAttribute("open");
  expect(view.container.querySelector("script")).toBeNull();
  expect(screen.getByText("private <script>alert(1)</script>")).toBeInTheDocument();
});
