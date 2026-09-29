import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { CodeBlock } from "./CodeBlock";

test("reports clipboard refusal without claiming code was copied", async () => {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockRejectedValue(new Error("refused")) },
  });
  render(
    <CodeBlock>
      <code>safe text</code>
    </CodeBlock>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Copy failed"));
  expect(screen.queryByRole("button", { name: "Copied" })).toBeNull();
});
