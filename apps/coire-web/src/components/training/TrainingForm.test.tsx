import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { evaluatedTrainingSpec, formSubmission, TrainingForm } from "./TrainingForm";
import { trainingDataset, trainingSpec } from "../../test/trainingFixtures";
import { mixtureError } from "./MixtureEditor";
import * as client from "../../api/training";
vi.mock("../../api/evaluations", () => ({ listEvaluationSuites: () => new Promise(() => {}) }));
vi.mock("./RegistryBinding", () => ({ RegistryBinding: () => <p>Registry binding</p> }));
vi.mock("./RecipePicker", () => ({ RecipePicker: () => <p>Recipes</p> }));
afterEach(() => vi.restoreAllMocks());
test("form preview preserves scalar types and exact source / typed intent equality", () => {
  const submission = formSubmission(trainingSpec); expect(submission.source_kind).toBe("form"); expect(JSON.parse(submission.source_yaml)).toEqual(submission.form_spec); expect(submission.source_yaml).not.toContain("placeholder"); expect(submission.form_spec?.seed).toBe(0);
});
test("unsupported objectives, schedules and two-rank execution are not offered as executable", () => {
  render(<TrainingForm datasets={[trainingDataset]} onSubmitted={vi.fn()}/>);
  expect(screen.getByRole("option", { name: /Two Studios/ })).toBeDisabled(); expect(screen.queryByRole("option", { name: /DPO|ORPO|cosine/i })).not.toBeInTheDocument(); expect(screen.getByRole("button", { name: "Submit training run" })).toBeDisabled(); expect(screen.getByLabelText("Training seed")).toHaveValue(0);
});
test("YAML validates through the same typed endpoint and missing evidence stays non-executable", async () => {
  const validate = vi.spyOn(client, "validateTraining").mockResolvedValue({ spec: trainingSpec, intent_sha256: "a".repeat(64), ready_to_run: false, reasons: ["profile_missing"] });
  render(<TrainingForm datasets={[trainingDataset]} onSubmitted={vi.fn()}/>); fireEvent.click(screen.getByRole("button", { name: "YAML recipe" }));
  fireEvent.change(screen.getByLabelText("Original YAML recipe"), { target: { value: "# exact comment\n" + JSON.stringify(trainingSpec) } }); fireEvent.click(screen.getByRole("button", { name: "Validate and resolve" }));
  await waitFor(() => expect(screen.getByText("profile missing")).toBeInTheDocument()); expect(validate).toHaveBeenCalledWith({ source_kind: "yaml", source_yaml: "# exact comment\n" + JSON.stringify(trainingSpec) }); expect(screen.getByRole("button", { name: "Submit training run" })).toBeDisabled();
});
test("invalid mixtures fail explicitly rather than normalizing user proportions", () => {
  const mixture = trainingSpec.data.train; expect(mixtureError(mixture)).toBeNull(); expect(mixtureError({ ...mixture, datasets: [{ ...mixture.datasets[0], mixture_proportion: 0.7 }] })).toMatch("sum to one"); expect(mixtureError({ ...mixture, epoch_samples: 100 })).toMatch("exceed"); expect(mixtureError({ ...mixture, datasets: [...mixture.datasets, ...mixture.datasets] })).toMatch("only once");
});
test("declared suites opt into numeric v2 without changing held-out loss", () => {
  const declared = evaluatedTrainingSpec(trainingSpec, [{ suite_id: "task-check", suite_version: 3, checkpoint_updates: [8, 16] }]);
  expect(declared.schema_version).toBe(2);
  expect(declared.eval?.loss_every_updates).toBe(trainingSpec.eval?.loss_every_updates);
  expect(JSON.parse(formSubmission(declared).source_yaml)).toEqual(declared);
  expect(evaluatedTrainingSpec(declared, [])).toEqual(trainingSpec);
});
