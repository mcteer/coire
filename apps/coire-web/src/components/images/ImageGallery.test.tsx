import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ImageGallery } from "./ImageGallery";
import type { ImageOutput } from "../../api/images";

const output = {
  id: "6c5c5686-52bc-4884-a941-f5fae37db913",
  tag: "normal",
  created_at: "2026-10-01T12:00:00Z",
  index: 0,
  job_id: "01J00000000000000000000000",
  byte_count: 100,
  file_sha256: "a".repeat(64),
  recipe: {},
} as ImageOutput;

vi.mock("../../api/images", () => ({
  downloadImageOutput: vi.fn(async () => {
    throw new Error("offline");
  }),
}));

afterEach(() => vi.restoreAllMocks());

test("keeps private gallery actions usable when a preview fails", async () => {
  const onTagChange = vi.fn();
  const onDownload = vi.fn();
  const onDelete = vi.fn(async () => {});
  const onReuse = vi.fn();
  const onRegenerate = vi.fn();
  render(
    <ImageGallery
      outputs={[output]}
      loading={false}
      tag=""
      onTagChange={onTagChange}
      nextCursor={null}
      busyId={null}
      onDownload={onDownload}
      onDelete={onDelete}
      onLoadOlder={() => {}}
      onReuse={onReuse}
      onRegenerate={onRegenerate}
    />,
  );

  await screen.findByText("Preview unavailable");
  expect(screen.getByRole("button", { name: "Retry preview" })).toBeEnabled();
  fireEvent.change(screen.getByRole("combobox", { name: "Image tag" }), {
    target: { value: "unknown" },
  });
  expect(onTagChange).toHaveBeenCalledWith("unknown");
  fireEvent.click(screen.getByRole("button", { name: `Download image ${output.id}` }));
  expect(onDownload).toHaveBeenCalledWith(output);
  fireEvent.click(screen.getByRole("button", { name: `Reuse settings from image ${output.id}` }));
  fireEvent.click(screen.getByRole("button", { name: `Regenerate image ${output.id}` }));
  fireEvent.click(screen.getByRole("button", { name: `Generate new seed from image ${output.id}` }));
  expect(onReuse).toHaveBeenCalledWith(output);
  expect(onRegenerate).toHaveBeenNthCalledWith(1, output, false);
  expect(onRegenerate).toHaveBeenNthCalledWith(2, output, true);
  fireEvent.click(screen.getByRole("button", { name: `Delete image ${output.id}` }));
  fireEvent.click(screen.getByRole("button", { name: `Delete image ${output.id}` }));
  await waitFor(() => expect(onDelete).toHaveBeenCalledWith(output.id));
});
