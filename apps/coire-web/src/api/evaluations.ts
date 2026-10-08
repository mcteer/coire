/** Durable admin evaluation clients; every wire shape is generated from OpenAPI. */
import { api } from "./client";
import type { SseFrame } from "./eventStream";
import type { components, operations } from "./schema";

export type EvaluationSuite = components["schemas"]["EvaluationSuite"];
export type EvaluationTemplate = components["schemas"]["EvaluationSuiteTemplate"];
export type EvaluationRegistration = components["schemas"]["EvaluationSuiteRegistration"];
export type EvaluationSubmission = components["schemas"]["EvaluationSubmission"];
export type EvaluationRun = components["schemas"]["EvaluationRunDetail"];
export type EvaluationReceipt = components["schemas"]["EvaluationReceipt"];
export type EvaluationResult = components["schemas"]["EvaluationResult"];
export type EvaluationComparison = components["schemas"]["EvaluationComparison"];
export type EvaluationGroup = components["schemas"]["EvaluationGroupDetail"];
export type EvaluationGroupLink = components["schemas"]["EvaluationGroupLink"];
export type EvaluationEvent = components["schemas"]["EvaluationEvent"];
export type EvaluationGroupEvent = components["schemas"]["EvaluationGroupEvent"];
export type EvaluationMeasurement = components["schemas"]["EvaluationMeasurement"];
export type EvaluationMeasurementDetail = components["schemas"]["EvaluationMeasurementDetail"];
export type EvaluationMeasurementRequest = components["schemas"]["EvaluationMeasurementRequest"];
export type EvaluationEvidence = components["schemas"]["EvaluationWorkerResult"];
export type EvaluationHistoryFilters = NonNullable<operations["list_runs_api_v1_admin_evaluations_get"]["parameters"]["query"]>;

const path = "/api/v1/admin/evaluations";
const suites = "/api/v1/admin/evaluation-suites";
const measurements = "/api/v1/admin/evaluation-measurements";
function query(values: Record<string, string | number | null | undefined>) {
  const result = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== null) result.set(key, String(value));
  return result.toString();
}
function mutation(method: string, body: unknown, key: string): RequestInit {
  return { method, body: JSON.stringify(body), headers: { "Idempotency-Key": key } };
}
export const listEvaluationTemplates = () => api<components["schemas"]["EvaluationTemplatePage"]>("/api/v1/admin/evaluation-suite-templates");
export const listEvaluationSuites = (cursor?: string | null, includeRetired = false) => api<components["schemas"]["EvaluationSuitePage"]>(`${suites}?${query({ limit: 100, cursor, include_retired: String(includeRetired) })}`);
export const getEvaluationSuite = (id: string, version: number) => api<EvaluationSuite>(`${suites}/${encodeURIComponent(id)}/versions/${version}`);
export const registerEvaluationSuite = (body: EvaluationRegistration, key: string) => api<EvaluationSuite>(suites, mutation("POST", body, key));
export const retireEvaluationSuite = (suite: EvaluationSuite, key: string) => api<EvaluationSuite>(`${suites}/${encodeURIComponent(suite.suite_id)}/versions/${suite.version}/retire`, mutation("POST", { expected_version: suite.registry_version } satisfies components["schemas"]["EvaluationControl"], key));
export const submitEvaluation = (body: EvaluationSubmission, key: string) => api<EvaluationReceipt>(path, mutation("POST", body, key));
export const listEvaluations = (filters: EvaluationHistoryFilters = {}) => api<components["schemas"]["EvaluationRunPage"]>(`${path}?${query({ limit: 25, ...filters })}`);
export const getEvaluation = (id: string) => api<EvaluationRun>(`${path}/${encodeURIComponent(id)}`);
export const cancelEvaluation = (id: string, version: number, key: string) => api<EvaluationReceipt>(`${path}/${encodeURIComponent(id)}/cancel`, mutation("POST", { expected_version: version } satisfies components["schemas"]["EvaluationControl"], key));
export const rerunEvaluation = (id: string, version: number, key: string) => api<EvaluationReceipt>(`${path}/${encodeURIComponent(id)}/rerun`, mutation("POST", { expected_version: version } satisfies components["schemas"]["EvaluationControl"], key));
export const getEvaluationEvidence = (id: string, evidenceId: string) => api<EvaluationEvidence>(`${path}/${encodeURIComponent(id)}/evidence/${encodeURIComponent(evidenceId)}`);
export const compareEvaluations = (left: string, leftSubject: number, right: string, rightSubject: number) => api<EvaluationComparison>(`/api/v1/admin/evaluation-comparisons?${query({ left_result_id: left, left_subject: leftSubject, right_result_id: right, right_subject: rightSubject })}`);
export const getEvaluationGroup = (id: string) => api<EvaluationGroup>(`/api/v1/admin/evaluation-groups/${encodeURIComponent(id)}`);
export const evaluationEventsUrl = (id: string) => `${path}/${encodeURIComponent(id)}/events/stream`;
export const evaluationGroupEventsUrl = (id: string) => `/api/v1/admin/evaluation-groups/${encodeURIComponent(id)}/events/stream`;
export const replayEvaluationEvents = (id: string, after = 0) => api<components["schemas"]["EvaluationReplayPage"]>(`${path}/${encodeURIComponent(id)}/events?after=${after}`);
export const replayEvaluationGroupEvents = (id: string, after = 0) => api<components["schemas"]["EvaluationGroupReplayPage"]>(`/api/v1/admin/evaluation-groups/${encodeURIComponent(id)}/events?after=${after}`);
export const measureEvaluation = (body: EvaluationMeasurementRequest, key: string) => api<EvaluationMeasurement>(measurements, mutation("POST", body, key));
export const getEvaluationMeasurement = (id: string) => api<EvaluationMeasurementDetail>(`${measurements}/${encodeURIComponent(id)}`);
export const cancelEvaluationMeasurement = (id: string, version: number, key: string) => api<EvaluationMeasurement>(`${measurements}/${encodeURIComponent(id)}/cancel`, mutation("POST", { expected_version: version } satisfies components["schemas"]["EvaluationControl"], key));

export function decodeEvaluationEvent(frame: SseFrame, id: string): EvaluationEvent | undefined {
  if (frame.event === null || !["state", "progress", "terminal", "reset"].includes(frame.event)) return undefined;
  const event = JSON.parse(frame.data) as EvaluationEvent;
  if (event.evaluation_id !== id || event.kind !== frame.event || !Number.isSafeInteger(event.sequence) || event.sequence < 1 || (event.kind === "reset" && event.snapshot?.id !== id)) throw new Error("Evaluation stream identity differs");
  return event;
}
export function decodeEvaluationGroupEvent(frame: SseFrame, id: string): EvaluationGroupEvent | undefined {
  if (frame.event === null || !["state", "terminal", "reset"].includes(frame.event)) return undefined;
  const event = JSON.parse(frame.data) as EvaluationGroupEvent;
  if (event.group_id !== id || event.kind !== frame.event || !Number.isSafeInteger(event.sequence) || event.sequence < 1 || (event.kind === "reset" && event.snapshot?.id !== id)) throw new Error("Evaluation group stream identity differs");
  return event;
}
