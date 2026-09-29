import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ChatAttachment, ChatAttachmentSelection } from "../../api/chat";
import { AttachmentList } from "./AttachmentList";

const conversationId = "00000000-0000-0000-0000-000000000001";
const fileId = "00000000-0000-0000-0000-000000000002";
const assetId = "00000000-0000-0000-0000-000000000003";
const pdf: ChatAttachment = {
  id: fileId,
  conversation_id: conversationId,
  owner_id: "00000000-0000-0000-0000-000000000004",
  filename: "scanned.pdf",
  detected_type: "application/pdf",
  original_bytes: 120,
  original_sha256: "a".repeat(64),
  derived_bytes: 500,
  state: "ready",
  page_count: 12,
  previews: [{ id: assetId, media_type: "image/png", width: 100, height: 100, page: 3 }],
  created_at: "2026-09-28T00:00:00Z",
  updated_at: "2026-09-28T00:00:00Z",
};

test("requires explicit PDF page choices and uses private preview and original routes", () => {
  const process = vi.fn();
  const choose = vi.fn();
  const view = render(
    <AttachmentList
      conversationId={conversationId}
      attachments={[pdf]}
      selections={[{ file_id: fileId, mode: "visual", pages: [3] }]}
      busy={false}
      onUpload={vi.fn()}
      onProcess={process}
      onSelect={choose}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("Included pages: 3");
  expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute(
    "href",
    `/api/v1/chat/conversations/${conversationId}/files/${fileId}/content`,
  );
  const preview = screen.getByRole("img", { name: "Page 3 of scanned.pdf" });
  expect(preview).toHaveAttribute(
    "src",
    `/api/v1/chat/conversations/${conversationId}/files/${fileId}/previews/${assetId}`,
  );
  expect(view.container.querySelector("embed, object, iframe")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Prepare selected pages" }));
  expect(process).toHaveBeenCalledWith(fileId, "render", [3]);
  fireEvent.click(screen.getByLabelText("5"));
  expect(choose).toHaveBeenCalledWith(
    { file_id: fileId, mode: "visual", pages: [3, 5] } satisfies ChatAttachmentSelection,
    fileId,
  );
});

test("shows failed extraction and offers an explicit retry", () => {
  const process = vi.fn();
  render(
    <AttachmentList
      conversationId={conversationId}
      attachments={[{ ...pdf, state: "failed", safe_error: "PDF is encrypted", previews: [] }]}
      selections={[]}
      busy={false}
      onUpload={vi.fn()}
      onProcess={process}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText(/PDF is encrypted/)).toBeInTheDocument();
  expect(screen.getByLabelText("scanned.pdf")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Retry processing" }));
  expect(process).toHaveBeenCalledWith(fileId, "inspect");
});
