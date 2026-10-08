import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import * as api from "../../api/evaluations";
import type { EvaluationGroupEvent, EvaluationGroupLink } from "../../api/evaluations";
import { CheckpointEvaluations } from "./CheckpointEvaluations";
let event: EvaluationGroupEvent | null = null;
vi.mock("../../hooks/useEventStream", () => ({ useEventStream: () => ({ data: event, connected: false, error: null }) }));
afterEach(() => { event = null; vi.restoreAllMocks(); });
test("checkpoint points preserve old attempts through rollback and show current operator ownership", () => {
  render(<CheckpointEvaluations currentCheckpoint="lower" links={[
    { trigger_id: "old", origin: "training_checkpoint", checkpoint_id: "higher", completed_update: 16, attempt_id: "attempt-old", fence: 1, state: "succeeded", trigger_phase: "complete", pause_owner: "released", resume_disposition: "resumed" },
    { trigger_id: "new", origin: "training_checkpoint", checkpoint_id: "lower", completed_update: 8, attempt_id: "attempt-new", fence: 2, state: "pending", trigger_phase: "pending_pause", pause_owner: "admin" },
  ]}/>);
  expect(screen.getByText(/higher · attempt attempt-old · fence 1/)).toBeInTheDocument();
  expect(screen.getByText(/lower · attempt attempt-new · fence 2/)).toBeInTheDocument();
  expect(screen.getByText(/Pause owner: admin/)).toBeInTheDocument();
  expect(screen.getByText(/Current recovery checkpoint: lower/)).toBeInTheDocument();
  expect(screen.queryByText(/delta/i)).not.toBeInTheDocument();
});
test("checkpoint reset after reconnect reconciles scores without reclaiming an administrator pause", async () => {
  const pending = { id: "group", origin: "training_checkpoint" as const, state: "pending" as const, created_at: "2026-10-07T00:00:00Z", runs: [] };
  const read = vi.spyOn(api, "getEvaluationGroup").mockResolvedValue(pending);
  const links: EvaluationGroupLink[] = [{ group_id: "group", trigger_id: "trigger", origin: "training_checkpoint", state: "pending", checkpoint_id: "checkpoint", completed_update: 8, attempt_id: "attempt", fence: 2, trigger_phase: "evaluating", pause_owner: "evaluation" }];
  const view = render(<CheckpointEvaluations links={links} currentCheckpoint="checkpoint"/>);
  await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
  event = { group_id: "group", sequence: 20, kind: "reset", state: "failed", created_at: pending.created_at, snapshot: { ...pending, state: "failed" } };
  view.rerender(<CheckpointEvaluations links={[{ ...links[0]!, pause_owner: "admin", trigger_phase: "cleaning_adapter", resume_disposition: "operator_override" }]} currentCheckpoint="checkpoint"/>);
  expect(await screen.findByText("Evaluation failed")).toBeInTheDocument();
  expect(screen.getByText(/Pause owner: admin.*resume: operator_override/)).toBeInTheDocument();
  expect(screen.getByText(/attempt attempt · fence 2/)).toBeInTheDocument();
});
