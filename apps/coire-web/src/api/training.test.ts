import { afterEach, expect, test, vi } from "vitest";
import { controlTraining, deleteDataset, uploadDataset, validateTraining } from "./training";
import { formSubmission } from "../components/training/TrainingForm";
import { trainingDataset, trainingJob, trainingSpec, trainingModelId, trainingVariantId } from "../test/trainingFixtures";
import { chatPickerSupportsExactSelection, type ChatPickerEntry } from "./chat";
afterEach(() => vi.unstubAllGlobals());
test("uploads only metadata and one bounded JSONL file with same-origin authority and idempotency", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ dataset_id: trainingDataset.id, state: "analyzing", version: 1 }))); vi.stubGlobal("fetch", fetchMock);
  const file = new File(['{"text":"sample"}\n'], "samples.jsonl");
  const metadata = { name: "samples", format: "text" as const, provenance: { source: "Synthetic", license_note: "CC0" }, analysis_model_id: trainingModelId, analysis_variant_id: trainingVariantId, split_seed: 0, validation_fraction: 0.05 };
  await uploadDataset(file, metadata, "command-key");
  const init = fetchMock.mock.calls[0][1]; expect(init.credentials).toBe("same-origin"); expect(init.headers).toEqual({ "Idempotency-Key": "command-key" }); expect([...init.body.keys()]).toEqual(["metadata", "file"]); expect(JSON.parse(init.body.get("metadata"))).toEqual(metadata);
});
test("rejects compressed/non-JSONL and empty uploads before any request", async () => {
  const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
  const metadata = { name: "bad", format: "text" as const, provenance: { source: "Fixture", license_note: "CC0" }, analysis_model_id: trainingModelId, analysis_variant_id: trainingVariantId, split_seed: 0, validation_fraction: 0.05 };
  await expect(uploadDataset(new File(["zip"], "data.jsonl.gz"), metadata, "key")).rejects.toThrow("JSONL"); await expect(uploadDataset(new File([], "data.jsonl"), metadata, "key")).rejects.toThrow("JSONL"); expect(fetchMock).not.toHaveBeenCalled();
});
test("sends optimistic versions and surfaces active-reference deletion refusal", async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response("{}" )).mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Dataset is pinned by a paused job" }), { status: 409 })); vi.stubGlobal("fetch", fetchMock);
  await controlTraining(trainingJob, "pause", "pause-key"); expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ expected_version: 1 });
  await expect(deleteDataset(trainingDataset, "delete-key")).rejects.toThrow("paused job");
});
test("form source and schema-backed spec are byte-preserved through validation", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ spec: trainingSpec, intent_sha256: "a".repeat(64), reasons: ["profile_missing"], ready_to_run: false }))); vi.stubGlobal("fetch", fetchMock);
  const body = formSubmission(trainingSpec); expect(JSON.parse(body.source_yaml)).toEqual(body.form_spec); await validateTraining(body); expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(body);
});
test("adapter picker rows require the advertised pair and cannot silently select the base UUID", () => {
  const entry: ChatPickerEntry = { id: trainingModelId, source: "studio", display_name: "Adapter", load_state: "cold", size_class: "small", verified: false, accepts_images: false, target: { model_id: trainingModelId, variant_id: trainingVariantId, adapter_id: trainingDataset.id, base_manifest_sha256: "a".repeat(64), adapter_manifest_sha256: "b".repeat(64) } };
  expect(chatPickerSupportsExactSelection(entry)).toBe(false); expect(chatPickerSupportsExactSelection({ ...entry, id: `${trainingModelId}@tool-sft` })).toBe(true); expect(chatPickerSupportsExactSelection({ ...entry, target: null })).toBe(true);
});
