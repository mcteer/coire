import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { DatasetPanel } from "./DatasetPanel";
import { trainingDataset, trainingModelId, trainingVariantId } from "../../test/trainingFixtures";
import * as client from "../../api/training";
vi.mock("./RegistryBinding", () => ({ RegistryBinding: ({ onChange }: { onChange: (m: string, v: string) => void }) => <button type="button" onClick={() => onChange("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002")}>Bind registry variant</button> }));
afterEach(() => vi.restoreAllMocks());
test("analysis pending shows no fabricated token or duplicate statistics", async () => {
  vi.spyOn(client, "listDatasets").mockResolvedValue({ items: [{ ...trainingDataset, state: "analyzing", analysis_id: "analysis" }] }); vi.spyOn(client, "getDatasetAnalysis").mockResolvedValue({ id: "analysis", dataset_id: trainingDataset.id, model_id: trainingModelId, variant_id: trainingVariantId, state: "queued", row_count: 0, invalid_count: 0, duplicate_rows: 0, created_at: trainingDataset.created_at });
  render(<DatasetPanel/>); expect(await screen.findByText(/Statistics unavailable until successful/)).toBeInTheDocument(); expect(screen.queryByText(/Tokens p50/)).not.toBeInTheDocument();
});
test("row errors stay bounded, reanalysis pins the selected tokenizer variant", async () => {
  vi.spyOn(client, "listDatasets").mockResolvedValue({ items: [{ ...trainingDataset, state: "analysis_failed", invalid_count: 1, diagnostics: [{ row: 7, field: "messages[0]", code: "unsupported_image" }] }] }); const analyze = vi.spyOn(client, "analyzeDataset").mockResolvedValue({ analysis_id: "analysis", state: "queued" });
  render(<DatasetPanel/>); expect(await screen.findByText(/Row 7.*unsupported image/)).toBeInTheDocument(); fireEvent.click(screen.getByRole("button", { name: "Bind registry variant" })); fireEvent.click(screen.getByRole("button", { name: /Reanalyze/ })); await waitFor(() => expect(analyze).toHaveBeenCalledWith(trainingDataset.id, { model_id: trainingModelId, variant_id: trainingVariantId }, expect.any(String)));
});
test("active-reference deletion refusal remains visible and preserves the dataset", async () => {
  vi.spyOn(client, "listDatasets").mockResolvedValue({ items: [trainingDataset] }); const remove = vi.spyOn(client, "deleteDataset").mockRejectedValue(new Error("Source pinned by paused job")); render(<DatasetPanel/>); const button = await screen.findByRole("button", { name: "Delete Tool corpus" }); fireEvent.click(button); expect(remove).not.toHaveBeenCalled(); fireEvent.click(button); expect(await screen.findByRole("alert")).toHaveTextContent("Source pinned by paused job"); expect(screen.getByRole("article", { name: "Tool corpus" })).toBeInTheDocument();
});
