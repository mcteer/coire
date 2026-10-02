import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { ImageModelList, ImagePresetList } from "../../api/images";
import { PresetEditor } from "./PresetEditor";

const { create, update, retire } = vi.hoisted(() => ({
  create: vi.fn(),
  update: vi.fn(),
  retire: vi.fn(),
}));
vi.mock("../../api/images", () => ({
  createImagePreset: create,
  updateImagePreset: update,
  retireImagePreset: retire,
}));

beforeEach(() => {
  vi.clearAllMocks();
});

const presets: ImagePresetList["items"] = [
  {
    id: "00000000-0000-0000-0000-000000000010",
    revision: 2,
    name: "Portrait",
    prompt_prefix: "",
    retired: false,
    defaults: {
      schema_version: 1,
      model_id: "00000000-0000-0000-0000-000000000002",
      prompt: "portrait",
    },
  },
];

test("shows presets without mutation controls when editing is disabled", () => {
  render(<PresetEditor presets={presets} canEdit={false} />);
  expect(screen.getByRole("region", { name: "Image presets" })).toBeInTheDocument();
  expect(screen.getByText(/Portrait/)).toBeInTheDocument();
  expect(screen.getByText(/revision 2/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /edit/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /retire/i })).not.toBeInTheDocument();
});

const models: ImageModelList["items"] = [
  {
    id: "00000000-0000-0000-0000-000000000002",
    display_name: "Image base",
    slug: "image-base",
    residency: "unknown",
    required_dependency_count: 0,
    loras: [],
    capability: {
      max_guidance: "5",
      max_height: 1024,
      max_loras: 0,
      max_outputs: 4,
      max_pixels: 1048576,
      max_steps: 50,
      max_width: 1024,
      min_guidance: "0",
      min_height: 64,
      min_steps: 1,
      min_width: 64,
      modes: ["txt2img"],
      supports_negative_prompt: false,
    },
  },
];

test("edits the source preset with its optimistic revision and preserves other defaults", async () => {
  update.mockResolvedValue(presets[0]);
  const changed = vi.fn().mockResolvedValue(undefined);
  render(<PresetEditor presets={presets} models={models} canEdit onChanged={changed} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit Portrait" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Preset name" }), {
    target: { value: "Portrait v2" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
  await waitFor(() => expect(update).toHaveBeenCalledOnce());
  expect(update).toHaveBeenCalledWith(presets[0].id, {
    expected_revision: 2,
    name: "Portrait v2",
    prompt_prefix: "",
    defaults: {
      ...presets[0].defaults,
      preset_id: null,
      preset_revision: null,
    },
  });
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
});

test("shows stale revision errors and allows a fresh save", async () => {
  update.mockRejectedValueOnce(new Error("stale revision")).mockResolvedValueOnce(presets[0]);
  render(<PresetEditor presets={presets} models={models} canEdit />);
  fireEvent.click(screen.getByRole("button", { name: "Edit Portrait" }));
  fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("stale revision");
  fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
  await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
});

test("creates a preset from an eligible model", async () => {
  create.mockResolvedValue(presets[0]);
  render(<PresetEditor presets={[]} models={models} canEdit />);
  fireEvent.change(screen.getByRole("textbox", { name: "Preset name" }), {
    target: { value: "Portrait" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Default prompt" }), {
    target: { value: "portrait" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Preset model" }), {
    target: { value: models[0].id },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
  await waitFor(() => expect(create).toHaveBeenCalledOnce());
  expect(create).toHaveBeenCalledWith({
    name: "Portrait",
    prompt_prefix: "",
    defaults: { schema_version: 1, model_id: models[0].id, prompt: "portrait" },
  });
});

test("retires only after confirmation and refreshes the list", async () => {
  retire.mockResolvedValue(undefined);
  const changed = vi.fn().mockResolvedValue(undefined);
  render(<PresetEditor presets={presets} models={models} canEdit onChanged={changed} />);
  fireEvent.click(screen.getByRole("button", { name: "Retire Portrait" }));
  expect(retire).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Retire Portrait" }));
  await waitFor(() => expect(retire).toHaveBeenCalledWith(presets[0].id));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
});
