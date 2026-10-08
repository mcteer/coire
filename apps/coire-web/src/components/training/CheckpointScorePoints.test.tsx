import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import type { EvaluationGroup } from "../../api/evaluations";
import { CheckpointScorePoints } from "./CheckpointScorePoints";

function group(state: string, mode = "deterministic"): EvaluationGroup {
  return { runs: [{ id: "run", state, suite: { suite_id: "suite", template: { mode } }, result: { outcome: state, aggregates: [0.25, null], subjects: [{ public_selector: "base" }, { public_selector: "adapter" }] } }] } as unknown as EvaluationGroup;
}
const link = { group_id: "group", origin: "training_checkpoint" as const, state: "succeeded" as const, completed_update: 16, attempt_id: "original-attempt", fence: 1 };
test("completed scores retain their original update and attempt without joining a curve", () => {
  const { container } = render(<CheckpointScorePoints group={group("succeeded")} link={link} maximumUpdate={32}/>);
  expect(screen.getByRole("img")).toHaveAccessibleName(/update 16, attempt original-attempt, fence 1/);
  expect(screen.getByText("0.250")).toBeInTheDocument();
  expect(container.querySelector("circle")).toHaveAttribute("cx", "305");
  expect(container.querySelectorAll("circle")).toHaveLength(1);
  expect(container.querySelector("polyline")).toBeNull();
});
test("partial failed outcomes and pairwise credits do not create quality points", () => {
  const view = render(<CheckpointScorePoints group={group("failed")} link={link} maximumUpdate={32}/>);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  view.rerender(<CheckpointScorePoints group={group("succeeded", "pairwise")} link={link} maximumUpdate={32}/>);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});
