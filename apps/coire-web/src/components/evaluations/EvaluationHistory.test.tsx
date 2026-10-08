import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import * as api from "../../api/evaluations";
import { EvaluationHistory } from "./EvaluationHistory";
afterEach(() => vi.restoreAllMocks());
test("denied history shows the authoritative refusal", async () => {
  vi.spyOn(api, "listEvaluations").mockRejectedValue(new Error("administrator scope required"));
  render(<EvaluationHistory/>);
  expect(await screen.findByRole("alert")).toHaveTextContent("administrator scope required");
});
test("empty history remains inspectable without new admission", async () => {
  vi.spyOn(api, "listEvaluations").mockResolvedValue({ items: [] });
  render(<EvaluationHistory/>);
  expect(await screen.findByText("No evaluation runs match these filters.")).toBeInTheDocument();
  expect(screen.getByLabelText("Evaluation outcome")).toBeInTheDocument();
});
test("an older filter response cannot replace the current refusal", async () => {
  let complete!: (value: Awaited<ReturnType<typeof api.listEvaluations>>) => void;
  vi.spyOn(api, "listEvaluations")
    .mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }))
    .mockRejectedValueOnce(new Error("administrator scope revoked"));
  render(<EvaluationHistory/>);
  fireEvent.change(screen.getByLabelText("Evaluation outcome"), { target: { value: "failed" } });
  expect(await screen.findByRole("alert")).toHaveTextContent("administrator scope revoked");
  await act(async () => complete({ items: [] }));
  expect(screen.getByRole("alert")).toHaveTextContent("administrator scope revoked");
  expect(screen.queryByText("No evaluation runs match these filters.")).not.toBeInTheDocument();
});
