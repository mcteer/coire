import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Comparison } from "./Comparison";
import type { EvaluationComparison } from "../../api/evaluations";
const base: EvaluationComparison = { left_result_id: "left", right_result_id: "right", left_subject: 0, right_subject: 1, comparable: true, left_score: 0.5, right_score: 0.75, delta: 0.25 };
describe("Evaluation comparison", () => {
  it("labels independent scores and signed delta", () => {
    render(<Comparison comparison={base}/>);
    expect(screen.getByText("+0.250")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Comparable evaluation scores" })).toBeInTheDocument();
  });
  it("does not display scores for incompatible results", () => {
    render(<Comparison comparison={{ ...base, comparable: false, reasons: ["runtime", "legacy_provenance"] }}/>);
    expect(screen.getByText(/runtime, legacy provenance/)).toBeInTheDocument();
    expect(screen.queryByText("+0.250")).not.toBeInTheDocument();
  });
  it("pairwise preferences never display an independent quality delta", () => {
    render(<Comparison comparison={{ ...base, pairwise: [{ case_id: "case", first_order: "AB", first: { winner: "A" }, second: { winner: "B" }, preferred_subject: 0 }] }}/>);
    expect(screen.getByText(/independent quality scores and quality deltas are not measured/)).toBeInTheDocument();
    expect(screen.getByText(/Left wins 1 · ties 0 · right wins 0/)).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Comparable evaluation scores" })).not.toBeInTheDocument();
    expect(screen.queryByText("+0.250")).not.toBeInTheDocument();
  });
});
