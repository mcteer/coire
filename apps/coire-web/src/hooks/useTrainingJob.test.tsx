import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { mergeTrainingMetrics, useTrainingJob } from "./useTrainingJob";
import * as api from "../api/training";
import { trainingJob, trainingJobId, trainingMetric, attemptId } from "../test/trainingFixtures";
import type { TrainingEvent } from "../api/training";
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function Probe() { const state = useTrainingJob(trainingJobId); return <output>{state.job?.state}:{state.job?.completed_update}:{state.metrics.map((m) => `${m.attempt_id}/${m.update}/${m.rolled_back}`).join(",")}:{state.streamError}</output>; }
function setup() {
  vi.spyOn(api, "getTrainingJob").mockResolvedValue(trainingJob);
  vi.spyOn(api, "listCheckpoints").mockResolvedValue({ items: [] });
  vi.spyOn(api, "listTrainingMetrics").mockResolvedValue({ items: [] });
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
  const fetchMock = vi.fn().mockResolvedValue(new Response(body)); vi.stubGlobal("fetch", fetchMock);
  const send = (event: TrainingEvent) => act(() => { controller.enqueue(new TextEncoder().encode(`id: ${event.id}\nevent: ${event.kind}\ndata: ${JSON.stringify(event)}\n\n`)); });
  return { send, fetchMock };
}
function progress(id: number, update: number): TrainingEvent { return { id, job_id: trainingJobId, attempt_id: attemptId, state_version: 1, occurred_at: "2026-10-04T00:00:00Z", kind: "progress", payload: { kind: "progress", metric: trainingMetric(update) } }; }
test("retains every batched event, ignores replay duplicates and separates optimizer attempts", async () => {
  const { send } = setup(); render(<Probe/>); await waitFor(() => expect(screen.getByText(/^running:0/)).toBeInTheDocument());
  send(progress(1, 1)); send(progress(2, 2)); send(progress(2, 2));
  await waitFor(() => expect(screen.getByText(new RegExp(`running:2:${attemptId}/1/false,${attemptId}/2/false`))).toBeInTheDocument());
  const merged = mergeTrainingMetrics([trainingMetric(2)], [trainingMetric(2, { rolled_back: true }), trainingMetric(2, { attempt_id: "01J00000000000000000000002" })]); expect(merged).toHaveLength(2); expect(merged[0].rolled_back).toBe(true);
});
test("reset rewinds to the snapshot and refetches persisted rolled-back history", async () => {
  const { send } = setup(); render(<Probe/>); await waitFor(() => expect(screen.getByText(/^running:0/)).toBeInTheDocument());
  send(progress(1, 20)); await waitFor(() => expect(screen.getByText(/^running:20/)).toBeInTheDocument());
  vi.mocked(api.getTrainingJob).mockResolvedValue({ ...trainingJob, version: 2, state: "recovering", completed_update: 10 });
  vi.mocked(api.listTrainingMetrics).mockResolvedValue({ items: [trainingMetric(20, { rolled_back: true })] });
  send({ id: 10, job_id: trainingJobId, state_version: 2, occurred_at: "2026-10-04T00:00:00Z", kind: "reset", payload: { kind: "reset", snapshot: { id: trainingJobId, version: 2, state: "recovering", completed_update: 10 } } });
  await waitFor(() => expect(screen.getByText(new RegExp(`recovering:10:${attemptId}/20/true`))).toBeInTheDocument());
});
test("rejects another job's events without changing progress", async () => {
  const { send } = setup(); render(<Probe/>); await waitFor(() => expect(screen.getByText(/^running:0/)).toBeInTheDocument()); send({ ...progress(1, 50), job_id: "01J00000000000000000000003" }); await waitFor(() => expect(screen.getByText(/Invalid training event identity/)).toBeInTheDocument()); expect(screen.getByText(/^running:0/)).toBeInTheDocument();
});
