import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import * as api from "../../api/evaluations";
import type { EvaluationGroup, EvaluationGroupEvent } from "../../api/evaluations";
import { EvaluationGroups } from "./EvaluationGroups";
let event: EvaluationGroupEvent | null = null;
vi.mock("../../hooks/useEventStream", () => ({ useEventStream: () => ({ data: event, connected: false, error: null }) }));
afterEach(() => { event = null; vi.restoreAllMocks(); });
test("pending obligation remains visible without a group or score", () => {
  render(<EvaluationGroups links={[{ trigger_id: "trigger", origin: "training_final", state: "pending", completed_update: 32 }]}/>);
  expect(screen.getByText("Durable evaluation obligation pending admission.")).toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});
test("reset snapshot reconciles evaluation completion after training stream ends", async () => {
  const pending: EvaluationGroup = { id: "group", origin: "training_final", state: "pending", created_at: "2026-10-07T00:00:00Z", runs: [] };
  const read = vi.spyOn(api, "getEvaluationGroup").mockResolvedValue(pending);
  const links = [{ group_id: "group", origin: "training_final" as const, state: "pending" as const }];
  const view = render(<EvaluationGroups links={links}/>);
  await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
  event = { group_id: "group", sequence: 8, state: "failed", kind: "reset", created_at: pending.created_at, snapshot: { ...pending, state: "failed" } };
  view.rerender(<EvaluationGroups links={links}/>);
  expect(await screen.findByText("Evaluation failed")).toBeInTheDocument();
  expect(screen.getByText(/independent of training success/)).toBeInTheDocument();
});
test("automatic group compares the exact base and adapter result through the API", async () => {
  const group = { id: "group", origin: "training_final", state: "succeeded", created_at: "2026-10-07T00:00:00Z", runs: [{ id: "run", state: "succeeded", suite: { suite_id: "task-final", version: 1 }, result: { id: "result", subjects: [{ display_name: "Base" }, { display_name: "Adapter" }] } }] } as unknown as EvaluationGroup;
  vi.spyOn(api, "getEvaluationGroup").mockResolvedValue(group);
  const compare = vi.spyOn(api, "compareEvaluations").mockResolvedValue({ left_result_id: "result", right_result_id: "result", left_subject: 0, right_subject: 1, comparable: true, left_score: 0.5, right_score: 0.75, delta: 0.25 });
  render(<EvaluationGroups links={[{ group_id: "group", origin: "training_final", state: "succeeded", completed_update: 4 }]}/>);
  fireEvent.click(await screen.findByRole("button", { name: "Compare base and adapter for task-final" }));
  expect(await screen.findByText("+0.250")).toBeInTheDocument();
  expect(compare).toHaveBeenCalledWith("result", 0, "result", 1);
  expect(screen.getByText(/independent of training success/)).toBeInTheDocument();
});
test("an infrastructure-failed comparison has no numeric delta", async () => {
  const group = { id: "group", origin: "training_final", state: "failed", created_at: "2026-10-07T00:00:00Z", runs: [{ id: "run", state: "failed", suite: { suite_id: "judge-final", version: 1 }, result: { id: "result", outcome: "failed", reason: "generation_failed", subjects: [{ display_name: "Base" }, { display_name: "Adapter" }] } }] } as unknown as EvaluationGroup;
  vi.spyOn(api, "getEvaluationGroup").mockResolvedValue(group);
  vi.spyOn(api, "compareEvaluations").mockResolvedValue({ left_result_id: "result", right_result_id: "result", left_subject: 0, right_subject: 1, comparable: false, reasons: ["outcome"] });
  render(<EvaluationGroups links={[{ group_id: "group", origin: "training_final", state: "failed", completed_update: 4 }]}/>);
  fireEvent.click(await screen.findByRole("button", { name: "Compare base and adapter for judge-final" }));
  expect(await screen.findByText(/No score delta is available: outcome/)).toBeInTheDocument();
  expect(screen.queryByRole("table", { name: "Comparable evaluation scores" })).not.toBeInTheDocument();
  expect(screen.getByText("Evaluation failed")).toBeInTheDocument();
});
