import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ChatConversation } from "../../api/chat";
import { ConversationHistory } from "./ConversationHistory";

const conversation: ChatConversation = {
  id: "00000000-0000-0000-0000-000000000001",
  owner_id: "00000000-0000-0000-0000-000000000002",
  title: "Saved notes",
  mode: "chat",
  selected_model_id: null,
  revision: 2,
  active_turn_id: null,
  created_at: "2026-09-28T00:00:00Z",
  updated_at: "2026-09-28T00:00:00Z",
};

test("opens an accessible history drawer and selects an owner conversation", () => {
  const open = vi.fn();
  render(
    <ConversationHistory
      conversations={[conversation]}
      selectedId={null}
      active={false}
      loading={false}
      hasMore={true}
      onOpen={open}
      onMore={() => {}}
    />,
  );
  const toggle = screen.getByRole("button", { name: "Conversations" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(screen.getByRole("button", { name: /Saved notes/ }));
  expect(open).toHaveBeenCalledWith(conversation.id);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
});

test("prevents history navigation during active generation", () => {
  render(
    <ConversationHistory
      conversations={[conversation]}
      selectedId={conversation.id}
      active
      loading={false}
      hasMore={false}
      onOpen={() => {}}
      onMore={() => {}}
    />,
  );
  expect(screen.getByRole("button", { name: /Saved notes/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: /Saved notes/ })).toHaveAttribute(
    "aria-current",
    "page",
  );
});
