import { afterEach, expect, test, vi } from "vitest";
import { cancelEvaluation, compareEvaluations, decodeEvaluationEvent, decodeEvaluationGroupEvent, evaluationEventsUrl, evaluationGroupEventsUrl, listEvaluations, rerunEvaluation, submitEvaluation } from "./evaluations";

afterEach(() => vi.unstubAllGlobals());

test("submits only declared registry references and reuses an explicit transport key", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "run", group_id: "group", version: 1, state: "queued" })));
  vi.stubGlobal("fetch", fetchMock);
  const body = { suite_id: "tasks", suite_version: 1, subjects: [{ model_id: "model", variant_id: "variant" }] };
  await submitEvaluation(body, "transport-key");
  expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/admin/evaluations");
  expect(fetchMock.mock.calls[0][1].credentials).toBe("same-origin");
  expect(fetchMock.mock.calls[0][1].headers["Idempotency-Key"]).toBe("transport-key");
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(body);
});

test("versions control cancellation and a rerun uses a distinct mutation operation", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response("{}")); vi.stubGlobal("fetch", fetchMock);
  await cancelEvaluation("run/a", 3, "cancel-key"); await rerunEvaluation("run/a", 3, "rerun-key");
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/v1/admin/evaluations/run%2Fa/cancel", "/api/v1/admin/evaluations/run%2Fa/rerun"]);
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ expected_version: 3 });
});

test("history and comparison encode scoped filters and both subject references", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response("{}")); vi.stubGlobal("fetch", fetchMock);
  await listEvaluations({ training_job_id: "job/a", cursor: "opaque/+" });
  const history = new URL(fetchMock.mock.calls[0][0], "https://test");
  expect(history.searchParams.get("training_job_id")).toBe("job/a"); expect(history.searchParams.get("cursor")).toBe("opaque/+");
  await compareEvaluations("left", 0, "right", 1);
  const comparison = new URL(fetchMock.mock.calls[1][0], "https://test");
  expect(comparison.pathname).toBe("/api/v1/admin/evaluation-comparisons");
  expect(Object.fromEntries(comparison.searchParams)).toEqual({ left_result_id: "left", left_subject: "0", right_result_id: "right", right_subject: "1" });
  expect(evaluationEventsUrl("run/a")).toBe("/api/v1/admin/evaluations/run%2Fa/events/stream");
  expect(evaluationGroupEventsUrl("group/a")).toBe("/api/v1/admin/evaluation-groups/group%2Fa/events/stream");
});

test("stream decoding keeps run/group sequences separate and rejects foreign history", () => {
  const run = { sequence: 4, evaluation_id: "run", kind: "terminal", state: "failed", version: 2, created_at: "now" };
  expect(decodeEvaluationEvent({ event: "terminal", id: "4", data: JSON.stringify(run) }, "run")).toEqual(run);
  expect(() => decodeEvaluationEvent({ event: "terminal", id: "4", data: JSON.stringify(run) }, "foreign")).toThrow("identity");
  const group = { sequence: 7, group_id: "group", kind: "reset", state: "running", snapshot: { id: "group" } };
  expect(decodeEvaluationGroupEvent({ event: "reset", id: "7", data: JSON.stringify(group) }, "group")).toEqual(group);
  expect(() => decodeEvaluationGroupEvent({ event: "reset", id: "7", data: JSON.stringify(group) }, "foreign")).toThrow("identity");
  expect(decodeEvaluationEvent({ event: "heartbeat", data: "{}", id: null }, "run")).toBeUndefined();
});
