import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
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
