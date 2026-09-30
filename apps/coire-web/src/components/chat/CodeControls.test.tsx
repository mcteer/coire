import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { CodeControls } from "./CodeControls";
import type { ChatTurn, RegisteredWorkspace } from "../../api/chat";

test("requires an explicit prior plan for Apply and registers a repository", async () => {
  const onAction = vi.fn();
  const onRegister = vi.fn().mockResolvedValue(true);
  const plan = {
    id: "turn-1",
    coding_call_id: "call-1",
    created_at: "2026-09-29T00:00:00Z",
  } as ChatTurn;
  const workspace = {
    id: "workspace-1",
    repository_url: "https://github.com/org/repo.git",
  } as RegisteredWorkspace;
  render(
    <CodeControls
      action="apply"
      onAction={onAction}
      workspaces={[workspace]}
      workspaceId={workspace.id}
      onWorkspace={vi.fn()}
      sourceRevision="HEAD"
      onRevision={vi.fn()}
      researchTurns={[]}
      researchId={null}
      onResearch={vi.fn()}
      planTurns={[plan]}
      planId={plan.coding_call_id ?? null}
      onPlan={vi.fn()}
      onRegister={onRegister}
      disabled={false}
    />,
  );
  expect(screen.getByRole("combobox", { name: "Plan to apply" })).toHaveValue("call-1");
  expect(screen.getByRole("textbox", { name: "Source revision" })).toBeDisabled();
  expect(screen.getByText(/fresh workspace snapshot/)).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Add HTTPS repository" }), {
    target: { value: "https://github.com/org/new.git" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  await waitFor(() => expect(onRegister).toHaveBeenCalledWith("https://github.com/org/new.git"));
  fireEvent.click(screen.getByRole("button", { name: "Research" }));
  expect(onAction).toHaveBeenCalledWith("research");
});
