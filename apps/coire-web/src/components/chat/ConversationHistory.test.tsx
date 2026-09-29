import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  const panel = screen.getByRole("complementary", { name: "Conversation history" });
  expect(panel).toHaveFocus();
  fireEvent.keyDown(panel, { key: "Escape" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(toggle).toHaveFocus();
  fireEvent.click(toggle);
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

test("renames with the saved revision and keeps the edit on refusal", async () => {
  const rename = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  render(
    <ConversationHistory
      conversations={[conversation]}
      selectedId={conversation.id}
      active={false}
      loading={false}
      hasMore={false}
      onOpen={() => {}}
      onMore={() => {}}
      onRename={rename}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Rename conversation" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Conversation title" }), {
    target: { value: "Updated notes" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save title" }));
  await waitFor(() => expect(rename).toHaveBeenCalledWith(conversation.id, "Updated notes", 2));
  expect(screen.getByRole("textbox", { name: "Conversation title" })).toHaveValue("Updated notes");
  fireEvent.click(screen.getByRole("button", { name: "Save title" }));
  await waitFor(() => expect(screen.queryByRole("textbox", { name: "Conversation title" })).toBeNull());
});

test("requires explicit confirmation before deleting the saved revision", async () => {
  const remove = vi.fn().mockResolvedValue(true);
  render(
    <ConversationHistory
      conversations={[conversation]}
      selectedId={conversation.id}
      active={false}
      loading={false}
      hasMore={false}
      onOpen={() => {}}
      onMore={() => {}}
      onDelete={remove}
    />,
  );
  const button = screen.getByRole("button", { name: "Delete conversation" });
  fireEvent.click(button);
  expect(remove).not.toHaveBeenCalled();
  expect(button).toHaveTextContent("Confirm delete Saved notes?");
  fireEvent.click(button);
  await waitFor(() => expect(remove).toHaveBeenCalledWith(conversation.id, 2));
});
