import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ChatMessage } from "../../api/chat";
import { Message } from "./Message";

const message: ChatMessage = {
  id: "00000000-0000-0000-0000-000000000001",
  conversation_id: "00000000-0000-0000-0000-000000000002",
  position: 2,
  role: "assistant",
  text: "<script>alert(1)</script>\n\n[bad](javascript:alert(1)) ![remote](https://example.test/track.png) [good](https://example.test/page)",
  reasoning: "",
  model_id: "00000000-0000-0000-0000-000000000003",
  model_display_name: "Older model name",
  attachment_ids: [],
  created_at: "2026-09-28T00:00:00Z",
};

test("shows snapshot attribution and keeps HTML, executable links and images inert", () => {
  const view = render(<Message message={message} />);
  expect(screen.getByText("Older model name")).toBeInTheDocument();
  expect(view.container.querySelector("script")).toBeNull();
  expect(view.container.querySelector("img")).toBeNull();
  expect(screen.getByText(/<script>alert/)).toBeInTheDocument();
  expect(screen.getByText("bad").closest("a")).toBeNull();
  expect(screen.getByRole("link", { name: "good" })).toHaveAttribute(
    "href",
    "https://example.test/page",
  );
});

test("keeps saved reasoning separate and collapsed from the answer", () => {
  const view = render(
    <Message message={{ ...message, text: "Public answer", reasoning: "Private steps" }} />,
  );
  expect(screen.getByText("Public answer")).toBeInTheDocument();
  expect(screen.getByText("Private steps")).toBeInTheDocument();
  expect(view.container.querySelector("details")).not.toHaveAttribute("open");
  expect(screen.getByText("Public answer").closest(".chat-reasoning")).toBeNull();
});

test("copies fenced code while keeping hostile links and remote images inert", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  const view = render(
    <Message
      message={{
        ...message,
        text: "```ts\nconst answer = 42;\n```\n\n[relative](//example.test/tracker) ![remote](https://example.test/pixel.png) [safe](/docs)",
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("const answer = 42;"));
  expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
  expect(screen.getByText("relative").closest("a")).toBeNull();
  expect(view.container.querySelector("img")).toBeNull();
  expect(screen.getByRole("link", { name: "safe" })).toHaveAttribute("href", "/docs");
});

test("shows the file and chosen content mode on a saved user message", () => {
  const fileId = "00000000-0000-0000-0000-000000000009";
  render(
    <Message
      message={{
        ...message,
        role: "user",
        text: "Summarize this",
        attachment_ids: [fileId],
        attachment_selections: [{ file_id: fileId, mode: "text", pages: [] }],
      }}
      attachments={[
        {
          id: fileId,
          owner_id: "00000000-0000-0000-0000-000000000010",
          conversation_id: message.conversation_id,
          filename: "notes.txt",
          detected_type: "text/plain",
          original_bytes: 5,
          original_sha256: "a".repeat(64),
          derived_bytes: 0,
          state: "ready",
          created_at: message.created_at,
          updated_at: message.created_at,
        },
      ]}
    />,
  );
  expect(screen.getByRole("list", { name: "Attached files" })).toHaveTextContent(
    "notes.txt · Extracted text",
  );
});
