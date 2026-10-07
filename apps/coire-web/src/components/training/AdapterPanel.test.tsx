import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AdapterPanel } from "./AdapterPanel";
import * as api from "../../api/training";
import { trainingJobId, trainingModelId, trainingVariantId } from "../../test/trainingFixtures";
import type { Adapter } from "../../api/training";
const adapter: Adapter = { id: "00000000-0000-4000-8000-000000000004", model_id: trainingModelId, base_variant_id: trainingVariantId, slug: "tool-sft", selector: `${trainingModelId}@tool-sft`, state: "ready", visibility: "admin_only", manifest_sha256: "a".repeat(64), base_manifest_sha256: "b".repeat(64), source_job_id: trainingJobId, source_checkpoint_id: "00000000-0000-4000-8000-000000000005", resolved_spec_sha256: "c".repeat(64), parameterization: "lora", objective: "sft", verified: false, version: 1, created_at: "2026-10-04T00:00:00Z" };
afterEach(() => vi.restoreAllMocks());
test("ready private adapter remains independently unverified and links only its exact selector", async () => {
  vi.spyOn(api, "listAdapters").mockResolvedValue({ items: [adapter] }); render(<AdapterPanel/>); expect(await screen.findByText(/private.*unverified/)).toBeInTheDocument(); expect(screen.getByRole("link", { name: "Select exact adapter in Chat" })).toHaveAttribute("href", `#chat/target/${encodeURIComponent(adapter.selector)}`); expect(screen.getByText(/feature 017/)).toBeInTheDocument();
});
test("publication carries current version; a base-publication refusal is shown instead of optimistic success", async () => {
  vi.spyOn(api, "listAdapters").mockResolvedValue({ items: [adapter] }); const publish = vi.spyOn(api, "curateAdapter").mockRejectedValue(new Error("Base model must be published")); render(<AdapterPanel/>); fireEvent.click(await screen.findByRole("button", { name: "Publish" })); await waitFor(() => expect(publish).toHaveBeenCalledWith(adapter, "published", expect.any(String))); expect(await screen.findByRole("alert")).toHaveTextContent("Base model must be published"); expect(screen.getByText(/private.*unverified/)).toBeInTheDocument();
});
