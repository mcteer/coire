import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ImageModelList, ImageSubmitRequest } from "../../api/images";
import { ImageForm } from "./ImageForm";

const modelId = "00000000-0000-0000-0000-000000000002";
const inputId = "00000000-0000-0000-0000-000000000042";
const models = [
  {
    id: modelId,
    display_name: "Local image model",
    capability: {
      modes: ["img2img"],
      min_width: 512,
      max_width: 512,
      min_height: 512,
      max_height: 512,
      min_steps: 1,
      max_steps: 20,
      max_outputs: 4,
    },
  },
] as ImageModelList["items"];

test("reused recipe stays editable and submits its hidden input binding", async () => {
  const onSubmit = vi.fn(async () => {});
  const request: ImageSubmitRequest = {
    schema_version: 1,
    model_id: modelId,
    mode: "img2img",
    prompt: "prefixed, direct subject",
    width: 512,
    height: 512,
    steps: 9,
    guidance: "0.125",
    seed: 7,
    n: 1,
    init_image_id: inputId,
    strength: "0.375",
    preset_id: null,
  };
  render(
    <ImageForm
      models={models}
      presets={[]}
      disabled={false}
      onSubmit={onSubmit}
      reuse={{ request, revision: 1 }}
    />,
  );
  const prompt = await screen.findByRole("textbox", { name: "Image prompt" });
  await waitFor(() => expect(prompt).toHaveValue("prefixed, direct subject"));
  expect(prompt).toHaveFocus();
  fireEvent.change(prompt, { target: { value: "prefixed, edited subject" } });
  fireEvent.click(screen.getByRole("button", { name: "Generate" }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit).toHaveBeenCalledWith(
    expect.objectContaining({
      model_id: modelId,
      prompt: "prefixed, edited subject",
      init_image_id: inputId,
      strength: "0.375",
      guidance: "0.125",
      seed: 7,
      preset_id: null,
    }),
  );
});

test("refuses a restored mode that the current model no longer supports", async () => {
  const onSubmit = vi.fn(async () => {});
  render(
    <ImageForm
      models={models}
      presets={[]}
      disabled={false}
      onSubmit={onSubmit}
      reuse={{
        request: { schema_version: 1, model_id: modelId, mode: "txt2img", prompt: "saved" },
        revision: 2,
      }}
    />,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "This model does not support the restored image mode",
  );
  expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
  expect(onSubmit).not.toHaveBeenCalled();
});
