import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import * as api from "../../api/evaluations";
import type { EvaluationRun } from "../../api/evaluations";
import { EvaluationDetail } from "./EvaluationDetail";
vi.mock("../../hooks/useEventStream", () => ({ useEventStream: () => ({ data: null, connected: false, error: null }) }));
afterEach(() => vi.restoreAllMocks());
function run(result: boolean): EvaluationRun {
  const target = { public_selector: "base@adapter", display_name: "Adapter", target: {}, runtime: {}, capability_profile: {}, context_window: 2048 };
  const suite = { suite_id: "task", version: 1, template: { kind: "task", mode: "deterministic" } };
  // Fixture intentionally includes only fields read by this presentation test.
  return { id: "run", group_id: "group", state: result ? "failed" : "queued", suite, subjects: [target], evidence: [{ id: "evidence", availability: "expired" }], result: result ? { outcome: "failed", aggregates: [0.9], subjects: [target], contamination: { status: "overlap", hit_count: 1, checked_cases: 16 } } : null } as unknown as EvaluationRun;
}
test("failed aggregate is hidden while contamination and expired evidence remain visible", async () => {
  vi.spyOn(api, "getEvaluation").mockResolvedValue(run(true));
  render(<EvaluationDetail id="run"/>);
  expect(await screen.findByText(/Training input overlap: overlap/)).toBeInTheDocument();
  expect(screen.getByText(/Evidence expired/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Read private evidence/ })).not.toBeInTheDocument();
  expect(screen.queryByText("0.900")).not.toBeInTheDocument();
});
test("pending evaluation has no aggregate and reconnects independently", async () => {
  vi.spyOn(api, "getEvaluation").mockResolvedValue(run(false));
  render(<EvaluationDetail id="run"/>);
  expect(await screen.findByText("Scores pending; no aggregate is available.")).toBeInTheDocument();
  expect(screen.getByText(/Refreshing persisted evaluation/)).toBeInTheDocument();
});
test("a late response for the previous evaluation cannot replace the selected run", async () => {
  let complete!: (value: EvaluationRun) => void;
  vi.spyOn(api, "getEvaluation").mockImplementation((id) => id === "old" ? new Promise((resolve) => { complete = resolve; }) : Promise.resolve({ ...run(false), id: "new" }));
  const view = render(<EvaluationDetail id="old"/>);
  view.rerender(<EvaluationDetail id="new"/>);
  expect(await screen.findByText("Scores pending; no aggregate is available.")).toBeInTheDocument();
  await act(async () => complete({ ...run(true), id: "old" }));
  expect(screen.queryByText(/Training input overlap/)).not.toBeInTheDocument();
  expect(screen.getByText("Scores pending; no aggregate is available.")).toBeInTheDocument();
});

test("uncertain cancellation retries the same version and operation key", async () => {
  const pending = { ...run(false), version: 7 };
  vi.spyOn(api, "getEvaluation").mockResolvedValue(pending);
  const cancel = vi.spyOn(api, "cancelEvaluation").mockRejectedValueOnce(new Error("transport uncertain")).mockResolvedValue({ id: "run", group_id: "group", state: "cancelling", version: 8, events_path: "/events" });
  render(<EvaluationDetail id="run"/>);
  fireEvent.click(await screen.findByRole("button", { name: "Cancel evaluation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("transport uncertain");
  fireEvent.click(screen.getByRole("button", { name: "Cancel evaluation" }));
  await waitFor(() => expect(cancel).toHaveBeenCalledTimes(2));
  expect(cancel.mock.calls[0]).toEqual(cancel.mock.calls[1]);
  expect(cancel.mock.calls[0]?.slice(0, 2)).toEqual(["run", 7]);
});

test("retired suites retain their history and disable fresh reruns", async () => {
  const previous = run(true);
  vi.spyOn(api, "getEvaluation").mockResolvedValue({ ...previous, suite: { ...previous.suite, retired: true } });
  render(<EvaluationDetail id="run"/>);
  expect(await screen.findByRole("button", { name: "Rerun with fresh evaluation ID" })).toBeDisabled();
  expect(screen.getByText(/historical results remain readable/)).toBeInTheDocument();
  expect(screen.getByText(/Training input overlap/)).toBeInTheDocument();
});
