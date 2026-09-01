import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AskCoire } from "./AskCoire";

const conversation = {
  id: "10000000-0000-4000-8000-000000000001",
  admin_user_id: "10000000-0000-4000-8000-000000000002",
  ops_session_id: "10000000-0000-4000-8000-000000000003",
  state: "active",
  degraded: false,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const issued = {
  proposal: {
    id: "10000000-0000-4000-8000-000000000004",
    conversation_id: conversation.id,
    ops_session_id: conversation.ops_session_id,
    proposer: "coire-ops:test",
    action: {
      operation: "instance.unload",
      target_type: "instance",
      target_id: "10000000-0000-4000-8000-000000000005",
      parameters: {},
      precondition: { resource_version: "v1", expected_state: "ready" },
    },
    rationale: "The instance is idle.",
    state: "pending",
    created_at: "2026-09-01T00:00:00Z",
    expires_at: "2026-09-01T00:05:00Z",
  },
  confirm_token: `coire_confirm_${"a".repeat(12)}_${"b".repeat(43)}`,
};

afterEach(() => vi.restoreAllMocks());

test("renders degraded answers and suppresses mutation controls", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(conversation), { status: 201 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "degraded",
            answer: "The admin model is unavailable.",
            observed_at: "2026-09-01T00:00:00Z",
            degraded: true,
            sources: ["cluster"],
            proposal: null,
          }),
          { status: 200 },
        ),
      ),
  );
  render(<AskCoire />);
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "What needs attention?" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(await screen.findByText("The admin model is unavailable.")).toBeInTheDocument();
  expect(screen.getByText(/Read-only degraded mode/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
});

test("renders the exact proposal and confirms with the opaque token and unchanged action", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(conversation), { status: 201 }))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          status: "proposed",
          answer: "I can propose unloading it.",
          observed_at: "2026-09-01T00:00:00Z",
          degraded: false,
          sources: ["cluster.instances"],
          proposal: issued,
        }),
        { status: 200 },
      ),
    )
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ ...issued.proposal, state: "executed" }), { status: 202 }),
    );
  vi.stubGlobal("fetch", fetch);
  render(<AskCoire />);
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "Unload the idle instance" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(await screen.findByText("instance.unload")).toBeInTheDocument();
  expect(screen.getByText(`instance:${issued.proposal.action.target_id}`)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Approve exact action" }));
  expect(await screen.findByText("executed")).toBeInTheDocument();
  const request = fetch.mock.calls[2][1] as RequestInit;
  expect(JSON.parse(String(request.body))).toEqual({
    confirm_token: issued.confirm_token,
    action: issued.proposal.action,
  });
});

test("declines a pending proposal without sending confirmation authority", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(conversation), { status: 201 }))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          status: "proposed",
          answer: "Review this proposal.",
          observed_at: "2026-09-01T00:00:00Z",
          degraded: false,
          sources: [],
          proposal: issued,
        }),
        { status: 200 },
      ),
    )
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ ...issued.proposal, state: "declined" }), { status: 200 }),
    );
  vi.stubGlobal("fetch", fetch);
  render(<AskCoire />);
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Unload it" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  fireEvent.click(await screen.findByRole("button", { name: "Decline" }));
  expect(await screen.findByText("declined")).toBeInTheDocument();
  expect(String((fetch.mock.calls[2][1] as RequestInit).body)).not.toContain("coire_confirm_");
});
