import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { TrainingControls } from "./TrainingControls";
import { trainingJob } from "../../test/trainingFixtures";
import * as api from "../../api/training";

afterEach(() => vi.restoreAllMocks());
test("evaluation cleanup prevents manual resume and lets an administrator keep training paused", async () => {
  const job = { ...trainingJob, state: "paused" as const, reason: "evaluation_pending" as const, evaluation_groups: [{ trigger_id: "trigger", origin: "training_checkpoint" as const, state: "pending" as const, trigger_phase: "cleaning_adapter" as const, pause_owner: "evaluation" as const }] };
  const control = vi.spyOn(api, "controlTraining").mockResolvedValue({ command_id: "command", job_id: job.id, version: 2, state: "paused" });
  render(<TrainingControls job={job} onChange={vi.fn().mockResolvedValue(undefined)}/>);
  expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
  expect(screen.getByText(/Evaluation pause: cleanup/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Keep paused after evaluation" }));
  await waitFor(() => expect(control).toHaveBeenCalledWith(job, "pause", expect.any(String)));
});
test("completed evaluation ownership allows an explicit administrator resume", () => {
  render(<TrainingControls job={{ ...trainingJob, state: "paused", reason: "admin_pause", evaluation_groups: [{ trigger_id: "trigger", origin: "training_checkpoint", state: "failed", trigger_phase: "complete", pause_owner: "released" }] }} onChange={vi.fn().mockResolvedValue(undefined)}/>);
  expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled();
});
