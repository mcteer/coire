/** Admin-only SFT clients. All wire shapes come from generated contracts. */
import { api, apiError } from "./client";
import type { components } from "./schema";

export type Dataset = components["schemas"]["DatasetDetail"];
export type DatasetAnalysis = components["schemas"]["DatasetAnalysis"];
export type DatasetUpload = components["schemas"]["DatasetUploadRequest"];
export type RegistryModel = components["schemas"]["Model"];
export type RegistryVariant = components["schemas"]["ModelVariant"];

const datasetsPath = "/api/v1/admin/datasets";
export const listDatasets = (cursor?: string | null) =>
  api<components["schemas"]["DatasetPage"]>(
    `${datasetsPath}?limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
export const getDataset = (id: string) => api<Dataset>(`${datasetsPath}/${encodeURIComponent(id)}`);
export const getDatasetAnalysis = (id: string) =>
  api<DatasetAnalysis>(`/api/v1/admin/dataset-analyses/${encodeURIComponent(id)}`);
export const listTrainingModels = () => api<RegistryModel[]>("/api/v1/admin/models?limit=100");
export const listTrainingVariants = (id: string) =>
  api<RegistryVariant[]>(`/api/v1/admin/models/${encodeURIComponent(id)}/variants`);

export async function uploadDataset(file: File, metadata: DatasetUpload, key: string) {
  if (!file.name.toLowerCase().endsWith(".jsonl") || file.size < 1 || file.size > 256 * 1024 ** 2)
    throw new Error("Choose an uncompressed JSONL file between 1 byte and 256 MiB.");
  const form = new FormData();
  form.set("metadata", JSON.stringify(metadata));
  form.set("file", file);
  const response = await fetch(datasetsPath, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Idempotency-Key": key },
    body: form,
  });
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as components["schemas"]["DatasetReceipt"];
}

export const analyzeDataset = (
  id: string,
  body: components["schemas"]["DatasetAnalyzeRequest"],
  key: string,
) =>
  api<components["schemas"]["DatasetAnalysisReceipt"]>(
    `${datasetsPath}/${encodeURIComponent(id)}/analyze`,
    {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": key },
    },
  );
export const deleteDataset = (dataset: Dataset, key: string) =>
  api<components["schemas"]["DatasetDeletionReceipt"]>(
    `${datasetsPath}/${encodeURIComponent(dataset.id)}`,
    {
      method: "DELETE",
      body: JSON.stringify({
        expected_version: dataset.version,
      } satisfies components["schemas"]["DatasetDeleteRequest"]),
      headers: { "Idempotency-Key": key },
    },
  );

export type TrainingSpec = components["schemas"]["TrainingSpec"];
export type TrainingSpecDocument = TrainingSpec | components["schemas"]["TrainingSpecV2"];
export type TrainingSubmission = components["schemas"]["TrainingSubmission"];
export type TrainingValidation = components["schemas"]["TrainingValidation"];
export type TrainingJob = components["schemas"]["TrainingJobDetail"];
export type TrainingEvent = components["schemas"]["TrainingEvent"];
export type TrainingMetric = components["schemas"]["TrainingMetricSample"];
export type Checkpoint = components["schemas"]["CheckpointDetail"];
export type Adapter = components["schemas"]["AdapterDetail"];
export type TrainingActivityItem = components["schemas"]["TrainingActivityItem"];
export const listTrainingActivity = (cursor?: string | null) =>
  api<components["schemas"]["CursorPage_TrainingActivityItem_"]>(
    `/api/v1/admin/console/training-activity?limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
const trainingPath = "/api/v1/admin/training";
export const listTrainingJobs = (cursor?: string | null) =>
  api<components["schemas"]["TrainingJobPage"]>(
    `${trainingPath}/jobs?limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
export const getTrainingJob = (id: string) =>
  api<TrainingJob>(`${trainingPath}/jobs/${encodeURIComponent(id)}`);
export const trainingEventsUrl = (id: string) =>
  `${trainingPath}/jobs/${encodeURIComponent(id)}/events`;
export const listTrainingMetrics = (id: string, cursor?: string | null) =>
  api<components["schemas"]["TrainingMetricPage"]>(
    `${trainingPath}/jobs/${encodeURIComponent(id)}/metrics?limit=2000${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
export const listCheckpoints = (id: string, cursor?: string | null) =>
  api<components["schemas"]["CheckpointPage"]>(
    `${trainingPath}/jobs/${encodeURIComponent(id)}/checkpoints?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
export const validateTraining = (body: TrainingSubmission) =>
  api<TrainingValidation>(`${trainingPath}/validate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const controlTraining = (
  job: Pick<TrainingJob, "id" | "version">,
  operation: "pause" | "resume" | "cancel",
  key: string,
) =>
  api<components["schemas"]["TrainingCommandReceipt"]>(
    `${trainingPath}/jobs/${encodeURIComponent(job.id)}/${operation}`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_version: job.version,
      } satisfies components["schemas"]["TrainingControlRequest"]),
      headers: { "Idempotency-Key": key },
    },
  );
export const deleteTrainingJob = (job: TrainingJob, key: string) =>
  api<components["schemas"]["TrainingDeletionReceipt"]>(
    `${trainingPath}/jobs/${encodeURIComponent(job.id)}`,
    {
      method: "DELETE",
      body: JSON.stringify({
        expected_version: job.version,
      } satisfies components["schemas"]["TrainingDeleteRequest"]),
      headers: { "Idempotency-Key": key },
    },
  );
export const listAdapters = (cursor?: string | null) =>
  api<components["schemas"]["AdapterPage"]>(
    `/api/v1/admin/adapters?limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
export const curateAdapter = (adapter: Adapter, visibility: Adapter["visibility"], key: string) =>
  api<components["schemas"]["AdapterReceipt"]>(
    `/api/v1/admin/adapters/${encodeURIComponent(adapter.id)}`,
    {
      method: "PATCH",
      body: JSON.stringify({
        expected_version: adapter.version,
        visibility,
      } satisfies components["schemas"]["AdapterCurationRequest"]),
      headers: { "Idempotency-Key": key },
    },
  );
export const retireAdapter = (adapter: Adapter, key: string) =>
  api<components["schemas"]["AdapterReceipt"]>(
    `/api/v1/admin/adapters/${encodeURIComponent(adapter.id)}`,
    {
      method: "DELETE",
      body: JSON.stringify({
        expected_version: adapter.version,
      } satisfies components["schemas"]["AdapterRetireRequest"]),
      headers: { "Idempotency-Key": key },
    },
  );

/** JSON is a YAML 1.2 subset: preserves all scalar types without a second parser. */
export const trainingYaml = (spec: TrainingSpecDocument): string => JSON.stringify(spec, null, 2) + "\n";
export const listTrainingRecipes = () =>
  api<components["schemas"]["TrainingRecipePage"]>(`${trainingPath}/recipes`);
export const submitTraining = (body: TrainingSubmission, key: string) =>
  api<components["schemas"]["TrainingJobReceipt"]>(`${trainingPath}/jobs`, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": key },
  });
export const promoteCheckpoint = (
  checkpoint: Checkpoint,
  body: components["schemas"]["CheckpointPromotionRequest"],
  key: string,
) =>
  api<components["schemas"]["AdapterReceipt"]>(
    `${trainingPath}/checkpoints/${encodeURIComponent(checkpoint.id)}/promote`,
    { method: "POST", body: JSON.stringify(body), headers: { "Idempotency-Key": key } },
  );
export type TrainingMeasurementRequest = components["schemas"]["TrainingMeasurementRequest"];
export type TrainingMeasurementResult = components["schemas"]["TrainingMeasurementResult"];
export type TrainingProfile = components["schemas"]["TrainingProfile"];
export const listTrainingProfiles = () =>
  api<components["schemas"]["TrainingProfilePage"]>(`${trainingPath}/profiles`);
export const submitTrainingMeasurement = (body: TrainingMeasurementRequest, key: string) =>
  api<components["schemas"]["TrainingMeasurementReceipt"]>(`${trainingPath}/measurements`, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": key },
  });
export const getTrainingMeasurement = (id: string) =>
  api<TrainingMeasurementResult>(`${trainingPath}/measurements/${encodeURIComponent(id)}`);
