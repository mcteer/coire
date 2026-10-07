import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { TrainingRunbook } from "./TrainingRunbook";

test("training alert links resolve to the packaged operational sections", () => {
  render(<TrainingRunbook isAdmin />);
  for (const id of [
    "baseline-wiring",
    "observe",
    "recover-and-fence",
    "checkpoints-and-retention",
    "kill-and-protect-chat",
  ]) {
    expect(document.getElementById(id)).toBeInTheDocument();
  }
  expect(screen.getByRole("link", { name: "Back to Training" })).toHaveAttribute(
    "href",
    "/#training",
  );
});

test("ordinary users cannot read administrative operating instructions", () => {
  render(<TrainingRunbook isAdmin={false} />);
  expect(screen.getByRole("heading", { name: "Admin access required" })).toBeInTheDocument();
  expect(document.getElementById("kill-and-protect-chat")).not.toBeInTheDocument();
});
