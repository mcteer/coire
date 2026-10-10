import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { PreferenceProbes } from "./TrainingRun";
import type { components } from "../../api/schema";

test("post-update probes show pair counts and remain separate from loss and rollback", () => {
  const sample: components["schemas"]["PreferenceMetricSample"] = {
    job_id: "01J00000000000000000000001",
    attempt_id: "01J00000000000000000000002",
    fence: 1,
    update: 8,
    objective: "orpo",
    implementation: "coire-preference-v1",
    normalization: "pair_mean",
    learning_rate: 0.00001,
    updates_per_second: 1,
    footprint_bytes: 100,
    peak_bytes: 200,
    kind: "train",
    loss: 0.3,
    pair_count: 4,
    response_tokens: 20,
    tokens_per_second: 10,
    probe: {
      scope: "held_out_post_update",
      sample_count: 8,
      accuracy: 0.75,
      margin: 0.5,
      chosen_nll: 0.2,
      odds_penalty: 0.1,
    },
    rolled_back: true,
    recorded_at: "2026-10-09T00:00:00Z",
  };
  render(<PreferenceProbes metrics={[sample]} />);
  expect(screen.getByText("ORPO · pair mean")).toBeInTheDocument();
  expect(screen.getByText("4 / 20")).toBeInTheDocument();
  expect(screen.getByText("0.75 / 0.5 (8 held-out pairs)")).toBeInTheDocument();
  expect(screen.getByText("0.2 / 0.1")).toBeInTheDocument();
  expect(screen.getByText("Rolled back")).toBeInTheDocument();
});
