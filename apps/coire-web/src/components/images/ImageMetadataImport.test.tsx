import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { ImageInput, ImageRecipeImport } from "../../api/images";
import { ImageMetadataImport } from "./ImageMetadataImport";

const { uploadImageRecipe, uploadImageInput, getImageInput, importImageRecipe } = vi.hoisted(
  () => ({
    uploadImageRecipe: vi.fn(),
    uploadImageInput: vi.fn(),
    getImageInput: vi.fn(),
    importImageRecipe: vi.fn(),
  }),
);
vi.mock("../../api/images", () => ({
  uploadImageRecipe,
  uploadImageInput,
  getImageInput,
  importImageRecipe,
}));

const input = {
  id: "00000000-0000-0000-0000-000000000043",
  state: "ready",
} as ImageInput;
const imported = {
  exact_reproduction_available: false,
  unavailable_reason: "Runtime version changed",
  missing_input_sha256: ["a".repeat(64)],
  missing_dependency_sha256: ["b".repeat(64)],
  recipe: {
    resolved: {
      inputs: [{ input_id: "source-1", sha256: "a".repeat(64), width: 64, height: 64 }],
      spec: { init_image_id: "source-1" },
    },
  },
  settings: {
    schema_version: 1,
    model_id: "00000000-0000-0000-0000-000000000002",
    prompt: "saved prompt",
  },
} as ImageRecipeImport;

beforeEach(() => {
  vi.clearAllMocks();
  uploadImageRecipe.mockResolvedValue(input);
  getImageInput.mockResolvedValue(input);
  importImageRecipe.mockResolvedValue(imported);
  uploadImageInput.mockResolvedValue({
    id: "replacement-1",
    state: "ready",
    purpose: "init",
    sha256: "a".repeat(64),
  });
});

test("drops a private PNG recipe and restores settings with environment warnings", async () => {
  const onImport = vi.fn();
  render(<ImageMetadataImport onImport={onImport} />);
  const file = new File(["png"], "saved.png", { type: "image/png" });
  fireEvent.drop(screen.getByText("Recipe PNG"), { dataTransfer: { files: [file] } });
  await waitFor(() => expect(importImageRecipe).toHaveBeenCalledWith(input.id));
  expect(uploadImageRecipe).toHaveBeenCalledWith(file);
  expect(importImageRecipe).toHaveBeenCalledWith(input.id);
  expect(onImport).not.toHaveBeenCalled();
  expect(screen.getByText(/Exact reproduction is unavailable/)).toBeInTheDocument();
  expect(screen.getByText(/Runtime version changed/)).toBeInTheDocument();
  expect(screen.getByText(/Missing source inputs/)).toBeInTheDocument();
  expect(screen.getByText(/Missing model components/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Restore with reattached inputs" })).toBeDisabled();
});

test("reattaches a missing source by digest before restoring settings", async () => {
  const onImport = vi.fn();
  importImageRecipe
    .mockResolvedValueOnce({ ...imported, missing_dependency_sha256: [] })
    .mockResolvedValueOnce({
      ...imported,
      missing_input_sha256: [],
      missing_dependency_sha256: [],
    });
  render(<ImageMetadataImport onImport={onImport} />);
  fireEvent.change(screen.getByLabelText("Recipe PNG"), {
    target: { files: [new File(["png"], "saved.png", { type: "image/png" })] },
  });
  const source = await screen.findByLabelText(`Reattach init image ${"a".repeat(12)}`);
  fireEvent.change(source, {
    target: { files: [new File(["image"], "source.png", { type: "image/png" })] },
  });
  const restore = await screen.findByRole("button", { name: "Restore with reattached inputs" });
  await waitFor(() => expect(restore).toBeEnabled());
  fireEvent.click(restore);
  await waitFor(() => expect(onImport).toHaveBeenCalledWith(imported.settings));
  expect(importImageRecipe).toHaveBeenLastCalledWith(input.id, {
    schema_version: 1,
    replacement_inputs: { ["a".repeat(64)]: "replacement-1" },
  });
});

test("shows a safe error and does not restore settings when recipe import fails", async () => {
  importImageRecipe.mockRejectedValue(new Error("Recipe version is unsupported"));
  const onImport = vi.fn();
  render(<ImageMetadataImport onImport={onImport} />);
  fireEvent.change(screen.getByLabelText("Recipe PNG"), {
    target: { files: [new File(["png"], "saved.png", { type: "image/png" })] },
  });
  expect(await screen.findByRole("alert")).toHaveTextContent("Recipe version is unsupported");
  expect(onImport).not.toHaveBeenCalled();
});
