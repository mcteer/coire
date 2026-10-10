import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ExportForm } from "./ExportForm";
import * as feedback from "../../api/feedback";

vi.mock("../training/RegistryBinding", () => ({
  RegistryBinding: ({ onChange }: { onChange: (model: string, variant: string) => void }) => (
    <button type="button" onClick={() => onChange("model", "variant")}>
      Bind analysis variant
    </button>
  ),
}));
afterEach(() => vi.restoreAllMocks());

function complete() {
  fireEvent.change(screen.getByRole("textbox", { name: "Dataset name" }), {
    target: { value: "Explicit pairs" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "License and consent note" }), {
    target: { value: "Local contribution policy" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Bind analysis variant" }));
}

test("submits server selection and warns about small samples and published retention", async () => {
  const submit = vi
    .spyOn(feedback, "submitFeedbackExport")
    .mockResolvedValue({ id: "export", state: "queued", version: 1 });
  const accepted = vi.fn();
  render(<ExportForm onSubmitted={accepted} />);
  expect(screen.getByText(/Fewer than 20 pairs/)).toBeVisible();
  expect(screen.getByText(/Published datasets and trained adapters remain/)).toBeVisible();
  complete();
  fireEvent.change(screen.getByRole("textbox", { name: "Judgement tag" }), {
    target: { value: "clear" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Export explicit pairs" }));
  await waitFor(() =>
    expect(submit).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "Explicit pairs",
        source: "owner_preferred",
        model_id: "model",
        variant_id: "variant",
        filters: expect.objectContaining({ tag: "clear" }),
      }),
      expect.any(String),
    ),
  );
  expect(accepted).toHaveBeenCalledWith(expect.objectContaining({ id: "export" }));
});

test("a retry of unchanged input keeps its immutable request identity", async () => {
  const submit = vi
    .spyOn(feedback, "submitFeedbackExport")
    .mockRejectedValueOnce(new Error("Lost reply"))
    .mockResolvedValue({ id: "export", state: "queued", version: 1 });
  render(<ExportForm onSubmitted={vi.fn()} />);
  complete();
  fireEvent.click(screen.getByRole("button", { name: "Export explicit pairs" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Lost reply");
  fireEvent.click(screen.getByRole("button", { name: "Export explicit pairs" }));
  await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
  expect(submit.mock.calls[1][1]).toBe(submit.mock.calls[0][1]);
});
