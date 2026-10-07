import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Training } from "./Training";
import { TrainingControls } from "../components/training/TrainingControls";
import { TrainingLoss } from "../components/training/TrainingRun";
import { TrainingMeasurements } from "../components/training/TrainingMeasurements";
import { trainingJob, trainingMetric, trainingSpec } from "../test/trainingFixtures";
import * as client from "../api/training";
afterEach(() => vi.restoreAllMocks());
test("ordinary-user denial does not request any privileged resources", () => { const request = vi.spyOn(client, "listTrainingJobs"); render(<Training isAdmin={false}/>); expect(screen.getByRole("heading", { name: "Admin access required" })).toBeInTheDocument(); expect(request).not.toHaveBeenCalled(); });
test("administrator pauses require manual resume; guard pauses show conditional automatic recovery", () => {
  const view = render(<TrainingControls job={{ ...trainingJob, state: "paused", reason: "admin_pause" }} onChange={vi.fn()}/>); expect(screen.getByText(/explicit Resume is required/)).toBeInTheDocument(); expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
  view.rerender(<TrainingControls job={{ ...trainingJob, state: "paused", reason: "latency_breach" }} onChange={vi.fn()}/>); expect(screen.getByText(/automatic resume requires cooldown/)).toBeInTheDocument();
});
test("cancel has inline confirmation and keeps uncertainty visible until authoritative stop", async () => {
  const control = vi.spyOn(client, "controlTraining").mockResolvedValue({ command_id: "id", job_id: trainingJob.id, state: "cancelling", version: 2 }); const refresh = vi.fn().mockResolvedValue(undefined);
  render(<TrainingControls job={trainingJob} onChange={refresh}/>); fireEvent.click(screen.getByRole("button", { name: /Stop tool-sft/ })); expect(control).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole("button", { name: /Stop tool-sft/ })); await waitFor(() => expect(refresh).toHaveBeenCalled()); expect(control).toHaveBeenCalledWith(trainingJob, "cancel", expect.any(String)); expect(screen.getByText(/termination is not yet confirmed/)).toBeInTheDocument();
});
test("loss is split by attempt and rolled-back segments have accessible status", () => {
  render(<TrainingLoss metrics={[trainingMetric(20, { rolled_back: true }), trainingMetric(10, { attempt_id: "01J00000000000000000000002" }), trainingMetric(10, { kind: "validation" })]}/>); expect(screen.getByRole("img", { name: /separated by attempt/ })).toBeInTheDocument(); fireEvent.click(screen.getByText(/Accessible loss samples/)); expect(screen.getByText("Rolled back")).toBeInTheDocument(); expect(screen.getAllByText("Retained")).toHaveLength(2);
});
test("missing, expired and insufficient evidence never claims a passing capability", async () => {
  vi.spyOn(client, "listTrainingProfiles").mockResolvedValue({ items: [] });
  render(<TrainingMeasurements validation={{ spec: trainingSpec, intent_sha256: "a".repeat(64), ready_to_run: false, reasons: ["profile_missing", "profile_expired", "insufficient_samples", "impossible_fit"] }}/>); expect(screen.getByText(/unmeasured combination/)).toBeInTheDocument(); expect(screen.getByText(/remeasure the exact runtime/)).toBeInTheDocument(); expect(screen.getByText(/missing traffic is not a passing latency/)).toBeInTheDocument(); expect(screen.getByText(/cannot fit even after/)).toBeInTheDocument(); await screen.findByText(/No measured profiles/);
});
test("missing profile routes disable measurement submission and report the integration prerequisite", async () => {
  vi.spyOn(client, "listTrainingProfiles").mockRejectedValue(new Error("route unavailable")); render(<TrainingMeasurements/>); expect(await screen.findByText(/Measurement and profile capability unavailable/)).toBeInTheDocument(); expect(screen.getByRole("button", { name: "Submit guarded measurement" })).toBeDisabled();
});
