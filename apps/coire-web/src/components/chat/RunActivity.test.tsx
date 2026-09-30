import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { RunActivity } from "./RunActivity";
import type { ChatTurnResult } from "../../api/chat";

afterEach(() => vi.unstubAllGlobals());

test("shows bounded tool activity and an owner bundle expiry", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ id: "artifact-1", expires_at: "2099-01-01T00:00:00Z" }), {
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const result = {
    type: "turn.result",
    tool: "apply",
    result: {
      branch: "coire/example",
      tests: { status: "passed", command: ["pytest", "-q"] },
      diff_excerpt: "+safe change",
      diff_truncated: false,
      artifact_id: "artifact-1",
    },
  } as ChatTurnResult;
  render(
    <RunActivity
      conversationId="conversation-1"
      turnId="turn-1"
      activity={Array.from({ length: 51 }, (_, index) => ({
        run_id: "run-1",
        sequence: index + 1,
        created_at: "2026-09-29T00:00:00Z",
        tool_name: "read_file",
        state: "completed",
      }))}
      result={result}
    />,
  );
  expect(screen.getByRole("button", { name: /Show earlier activity/ })).toHaveTextContent(
    "1 hidden",
  );
  expect(screen.getByText(/Tests passed/)).toBeInTheDocument();
  expect(screen.getByText("Command: pytest -q")).toBeInTheDocument();
  expect(await screen.findByText(/Bundle expires/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Download branch bundle" })).toHaveAttribute(
    "href",
    "/api/v1/chat/conversations/conversation-1/turns/turn-1/artifact",
  );
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
});
