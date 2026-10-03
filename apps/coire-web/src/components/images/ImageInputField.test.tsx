import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { ImageInputField } from "./ImageInputField";

const { uploadImageInput, getImageInput } = vi.hoisted(() => ({
  uploadImageInput: vi.fn(),
  getImageInput: vi.fn(),
}));
vi.mock("../../api/images", () => ({ uploadImageInput, getImageInput }));

beforeEach(() => vi.clearAllMocks());

test("uploads a private source image and binds only the ready input", async () => {
  const onChange = vi.fn();
  uploadImageInput.mockResolvedValue({ id: "input-1", state: "ready", purpose: "init" });
  render(<ImageInputField label="Source image" purpose="init" value={null} onChange={onChange} />);
  const file = new File(["image"], "source.png", { type: "image/png" });
  fireEvent.change(screen.getByLabelText("Source image"), { target: { files: [file] } });
  await waitFor(() => expect(onChange).toHaveBeenLastCalledWith("input-1"));
  expect(uploadImageInput).toHaveBeenCalledWith(file, "init");
  expect(getImageInput).not.toHaveBeenCalled();
});

test("shows a field-specific processing failure without binding an input", async () => {
  const onChange = vi.fn();
  uploadImageInput.mockResolvedValue({
    id: "input-2",
    state: "failed",
    purpose: "mask",
    safe_error: "invalid_mask",
  });
  render(<ImageInputField label="Mask image" purpose="mask" value={null} onChange={onChange} />);
  fireEvent.change(screen.getByLabelText("Mask image"), {
    target: { files: [new File(["image"], "mask.png", { type: "image/png" })] },
  });
  expect(await screen.findByRole("alert")).toHaveTextContent("Mask image: Error: invalid_mask");
  expect(onChange).toHaveBeenCalledWith(null);
  expect(onChange).not.toHaveBeenCalledWith("input-2");
});

test("refuses a reattached image whose normalized digest changed", async () => {
  const onChange = vi.fn();
  uploadImageInput.mockResolvedValue({
    id: "input-3",
    state: "ready",
    purpose: "init",
    sha256: "b".repeat(64),
  });
  render(
    <ImageInputField
      label="Reattach source"
      purpose="init"
      value={null}
      onChange={onChange}
      expectedSha256={"a".repeat(64)}
    />,
  );
  fireEvent.change(screen.getByLabelText("Reattach source"), {
    target: { files: [new File(["image"], "source.png", { type: "image/png" })] },
  });
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Image digest differs from the saved recipe",
  );
  expect(onChange).not.toHaveBeenCalledWith("input-3");
});
