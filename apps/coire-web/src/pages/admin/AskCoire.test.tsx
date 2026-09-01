import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Overview } from "../../App";
import { consoleSnapshot } from "../../test/fixtures";

afterEach(() => vi.restoreAllMocks());

test("renders grounded unavailable answers without mutation controls", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "unavailable",
          answer: "The admin model is unavailable.",
          observed_at: "2026-09-01T00:00:00Z",
          sources: ["cluster"],
        }),
        { status: 200 },
      ),
    ),
  );
  render(<Overview snapshot={consoleSnapshot()} />);
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "What needs attention?" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(await screen.findByText("The admin model is unavailable.")).toBeInTheDocument();
  expect(screen.getByText("Sources: cluster")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /confirm|apply|execute/i })).not.toBeInTheDocument();
});
