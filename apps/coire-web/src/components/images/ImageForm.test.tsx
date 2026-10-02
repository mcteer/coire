import type { ComponentProps } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ImageModelList, ImageSubmitRequest } from "../../api/images";
import { ImageForm as BaseImageForm } from "./ImageForm";

const limits = {
  generation_input_max_bytes: 10 * 1024 ** 2,
  recipe_input_max_bytes: 64 * 1024 ** 2,
  output_max_bytes: 64 * 1024 ** 2,
  owner_storage_quota_bytes: 5 * 1024 ** 3,
  pending_per_owner: 4,
  daily_outputs_per_owner: 100,
  output_retention_hours: null,
};

function ImageForm(props: ComponentProps<typeof BaseImageForm>) {
  return <BaseImageForm limits={limits} {...props} />;
}

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

test("names missing source and mask fields before fill submission", async () => {
  const onSubmit = vi.fn(async () => {});
  render(
    <ImageForm
      models={[
        {
          ...models[0],
          capability: { ...models[0].capability, modes: ["fill"] },
        },
      ] as ImageModelList["items"]}
      presets={[]}
      disabled={false}
      onSubmit={onSubmit}
      reuse={{
        request: { schema_version: 1, model_id: modelId, mode: "fill", prompt: "saved" },
        revision: 1,
      }}
    />,
  );
  expect(await screen.findByText("A source image is required for fill.")).toHaveAttribute(
    "role",
    "alert",
  );
  expect(screen.getByText("A mask image is required for fill.")).toHaveAttribute("role", "alert");
  expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
  expect(onSubmit).not.toHaveBeenCalled();
});

test("discloses configured retention, quota and upload bounds before generation", () => {
  render(
    <ImageForm
      models={models}
      presets={[]}
      disabled={false}
      onSubmit={vi.fn()}
      limits={{ ...limits, output_retention_hours: 12, owner_storage_quota_bytes: 1024 ** 3 }}
    />,
  );
  expect(screen.getByText(/automatically deleted after 12 hours/)).toBeInTheDocument();
  expect(screen.getByText(/storage quota: 1 GiB/)).toBeInTheDocument();
  expect(screen.getByText(/Generation inputs: 10 MiB/)).toHaveTextContent("recipe PNGs: 64 MiB");
});

test("default retention is until owner deletion, and absent policy refuses submission", async () => {
  const onSubmit = vi.fn(async () => {});
  const { rerender } = render(
    <ImageForm models={models} presets={[]} disabled={false} onSubmit={onSubmit} />,
  );
  expect(screen.getByText(/retained until you delete them/)).toBeInTheDocument();
  rerender(
    <ImageForm models={models} presets={[]} disabled={false} onSubmit={onSubmit} limits={null} />,
  );
  expect(screen.getByText(/Storage and retention policy is unavailable/)).toBeInTheDocument();
  const prompt = screen.getByRole("textbox", { name: "Image prompt" });
  fireEvent.change(prompt, { target: { value: "test" } });
  fireEvent.submit(prompt.closest("form")!);
  await waitFor(() => expect(onSubmit).not.toHaveBeenCalled());
  expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
});

test("selected LoRAs keep their order and exact scale strings", async () => {
  const onSubmit = vi.fn(async (request: ImageSubmitRequest) => {
    void request;
  });
  const adapters = [
    { id: "00000000-0000-0000-0000-000000000101", display_name: "First adapter" },
    { id: "00000000-0000-0000-0000-000000000102", display_name: "Second adapter" },
  ];
  render(
    <ImageForm
      models={[
        {
          ...models[0],
          capability: { ...models[0].capability, modes: ["txt2img"], max_loras: 2 },
          loras: adapters,
        },
      ] as ImageModelList["items"]}
      presets={[]}
      disabled={false}
      onSubmit={onSubmit}
    />,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Image prompt" }), {
    target: { value: "a private subject" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Add LoRA" }), {
    target: { value: adapters[1].id },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Add LoRA" }), {
    target: { value: adapters[0].id },
  });
  fireEvent.change(screen.getByRole("spinbutton", { name: "LoRA scale 1" }), {
    target: { value: "0.375125" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Generate" }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit.mock.calls.at(0)?.[0].loras).toEqual([
    { model_id: adapters[1].id, scale: "0.375125" },
    { model_id: adapters[0].id, scale: "1" },
  ]);
});

test("restored adapter refuses submission when it is no longer listed", async () => {
  const onSubmit = vi.fn(async () => {});
  render(
    <ImageForm
      models={[
        {
          ...models[0],
          capability: { ...models[0].capability, max_loras: 1 },
          loras: [],
        },
      ] as ImageModelList["items"]}
      presets={[]}
      disabled={false}
      onSubmit={onSubmit}
      reuse={{
        request: {
          schema_version: 1,
          model_id: modelId,
          mode: "img2img",
          prompt: "saved",
          init_image_id: inputId,
          strength: "0.5",
          loras: [{ model_id: "00000000-0000-0000-0000-000000000101", scale: "1" }],
        },
        revision: 1,
      }}
    />,
  );
  expect(await screen.findByText("A selected LoRA is no longer available.")).toHaveAttribute(
    "role",
    "alert",
  );
  expect(screen.getByRole("button", { name: "Generate" })).toBeDisabled();
  expect(onSubmit).not.toHaveBeenCalled();
});

test("selected upscale asset and factor are submitted together", async () => {
  const onSubmit = vi.fn(async (request: ImageSubmitRequest) => {
    void request;
  });
  const upscaleId = "00000000-0000-0000-0000-000000000103";
  render(
    <ImageForm
      models={[
        {
          ...models[0],
          capability: { ...models[0].capability, modes: ["txt2img"] },
          upscalers: [{ id: upscaleId, display_name: "SeedVR2" }],
        },
      ] as ImageModelList["items"]}
      presets={[]}
      disabled={false}
      onSubmit={onSubmit}
    />,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Image prompt" }), {
    target: { value: "a private subject" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Upscale model" }), {
    target: { value: upscaleId },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Upscale factor" }), {
    target: { value: "4" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Generate" }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit.mock.calls.at(0)?.[0].upscale).toEqual({ model_id: upscaleId, factor: 4 });
});
