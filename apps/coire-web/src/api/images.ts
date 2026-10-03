/** Typed private image API. */

import { api, apiError } from "./client";
import type { components } from "./schema";

export type ImageJob = components["schemas"]["ImageJob"];
export type ImageJobReceipt = components["schemas"]["ImageJobReceipt"];
export type ImageSubmitRequest = components["schemas"]["ImageSubmitRequest-Input"];
export type ImageJobPage = components["schemas"]["ImageJobPage"];
export type ImageJobEvent = components["schemas"]["ImageJobEvent"];
export type ImageOutput = components["schemas"]["ImageOutput"];
export type ImageOutputPage = components["schemas"]["ImageOutputPage"];
export type ImagePresetList = components["schemas"]["ImagePresetList"];
export type ImagePreset = components["schemas"]["ImagePreset"];
export type ImagePresetCreate = components["schemas"]["ImagePresetCreate"];
export type ImagePresetUpdate = components["schemas"]["ImagePresetUpdate"];
export type ImageModelList = components["schemas"]["ImageModelList"];
export type ImageRecipeImportRequest = components["schemas"]["ImageRecipeImportRequest"];
export type ImageRecipeImport = components["schemas"]["ImageRecipeImport"];
export type ImageInput = components["schemas"]["ImageInput"];
export type ImageDownloadGrant = components["schemas"]["ImageDownloadGrant"];
export type ImageDeletionReceipt = components["schemas"]["ImageDeletionReceipt"];
export type ImageActivityItem = components["schemas"]["ImageActivityItem"];
export type ImageActivityPage = components["schemas"]["CursorPage_ImageActivityItem_"];

export function requestFromImageOutput(output: ImageOutput, newSeed = false): ImageSubmitRequest {
  const spec = output.recipe.resolved.spec;
  return {
    ...spec,
    // The recipe already contains the effective prompt. Reapplying its original preset
    // would duplicate the prefix and can silently change settings after an edit.
    preset_id: null,
    preset_revision: null,
    seed: newSeed ? null : spec.seed,
  };
}

export function listAdminImageJobs(cursor: string | null = null): Promise<ImageActivityPage> {
  const query = new URLSearchParams({ limit: "50" });
  if (cursor) {
    const [before, beforeId] = cursor.split("|");
    if (!before || !beforeId) throw new Error("Image activity cursor is invalid");
    query.set("before", before);
    query.set("before_id", beforeId);
  }
  return api<ImageActivityPage>(`/api/v1/admin/image-jobs?${query}`);
}

export function killAdminImageJob(jobId: string): Promise<ImageActivityItem> {
  return api<ImageActivityItem>(`/api/v1/admin/image-jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
}

export function listImageModels(): Promise<ImageModelList> {
  return api<ImageModelList>("/api/v1/images/models");
}

export function listImagePresets(): Promise<ImagePresetList> {
  return api<ImagePresetList>("/api/v1/images/presets");
}

export function createImagePreset(body: ImagePresetCreate): Promise<ImagePreset> {
  return api<ImagePreset>("/api/v1/admin/image-presets", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateImagePreset(id: string, body: ImagePresetUpdate): Promise<ImagePreset> {
  return api<ImagePreset>(`/api/v1/admin/image-presets/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function retireImagePreset(id: string): Promise<void> {
  return api<void>(`/api/v1/admin/image-presets/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function listImageJobs(
  cursor: string | null = null,
  limit = 25,
  state?: ImageJob["state"],
): Promise<ImageJobPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  if (state) query.set("state", state);
  return api<ImageJobPage>(`/api/v1/images?${query}`);
}

export function getImageJob(jobId: string): Promise<ImageJob> {
  return api<ImageJob>(`/api/v1/images/${encodeURIComponent(jobId)}`);
}

export function submitImageJob(
  request: ImageSubmitRequest,
  idempotencyKey: string,
): Promise<ImageJobReceipt> {
  return api<ImageJobReceipt>("/api/v1/images", {
    method: "POST",
    body: JSON.stringify(request),
    headers: { "Idempotency-Key": idempotencyKey },
  });
}

export function cancelImageJob(jobId: string): Promise<ImageJob> {
  return api<ImageJob>(`/api/v1/images/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
}

export function imageJobEventsUrl(jobId: string): string {
  return `/api/v1/images/${encodeURIComponent(jobId)}/events`;
}

export function listImageOutputs(
  cursor: string | null = null,
  limit = 25,
  tag?: ImageOutput["tag"],
): Promise<ImageOutputPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  if (tag) query.set("tag", tag);
  return api<ImageOutputPage>(`/api/v1/image-outputs?${query}`);
}

export function getImageOutput(outputId: string): Promise<ImageOutput> {
  return api<ImageOutput>(`/api/v1/image-outputs/${encodeURIComponent(outputId)}`);
}

export function deleteImageOutput(outputId: string): Promise<ImageDeletionReceipt> {
  return api<ImageDeletionReceipt>(`/api/v1/image-outputs/${encodeURIComponent(outputId)}`, {
    method: "DELETE",
  });
}

export async function downloadImageOutput(outputId: string): Promise<Blob> {
  const path = `/api/v1/image-outputs/${encodeURIComponent(outputId)}/content`;
  const fetchWithFreshGrant = async (): Promise<Response> => {
    const grant = await api<ImageDownloadGrant>(
      `/api/v1/image-outputs/${encodeURIComponent(outputId)}/download-grants`,
      { method: "POST", body: "{}" },
    );
    const granted = new URL(grant.url, location.origin);
    const fragment = new URLSearchParams(granted.hash.slice(1));
    const token = fragment.get("grant");
    if (
      grant.output_id !== outputId ||
      granted.origin !== location.origin ||
      granted.pathname !== path ||
      granted.search ||
      fragment.size !== 1 ||
      !token
    ) {
      throw new Error("Image download grant is invalid");
    }
    return fetch(path, {
      credentials: "same-origin",
      headers: { "X-Coire-Image-Grant": token },
    });
  };
  let response = await fetchWithFreshGrant();
  if (response.status === 404) response = await fetchWithFreshGrant();
  if (!response.ok) throw await apiError(response);
  const blob = await response.blob();
  if (blob.type !== "image/png" || blob.size < 1 || blob.size > 64 * 1024 * 1024) {
    throw new Error("Image download is invalid");
  }
  return blob;
}

export function importImageRecipe(
  inputId: string,
  request: ImageRecipeImportRequest = { schema_version: 1 },
): Promise<ImageRecipeImport> {
  return api<ImageRecipeImport>(`/api/v1/image-inputs/${encodeURIComponent(inputId)}/recipe`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export async function uploadImageInput(
  file: File,
  purpose: ImageInput["purpose"],
): Promise<ImageInput> {
  const recipe = purpose === "recipe";
  const maxBytes = recipe ? 64 * 1024 * 1024 : 10 * 1024 * 1024;
  const supported = recipe
    ? file.type === "image/png" && file.name.toLowerCase().endsWith(".png")
    : ["image/png", "image/jpeg", "image/webp"].includes(file.type);
  if (file.size < 1 || file.size > maxBytes || !supported) {
    throw new Error(
      recipe
        ? "Choose a PNG recipe no larger than 64 MiB"
        : "Choose an image no larger than 10 MiB",
    );
  }
  const form = new FormData();
  form.set("purpose", purpose);
  form.set("filename", file.name);
  form.set("byte_count", String(file.size));
  form.set("file", file);
  const response = await fetch("/api/v1/image-inputs", {
    method: "POST",
    credentials: "same-origin",
    body: form,
  });
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as ImageInput;
}

export function uploadImageRecipe(file: File): Promise<ImageInput> {
  return uploadImageInput(file, "recipe");
}

export function getImageInput(inputId: string): Promise<ImageInput> {
  return api<ImageInput>(`/api/v1/image-inputs/${encodeURIComponent(inputId)}`);
}
