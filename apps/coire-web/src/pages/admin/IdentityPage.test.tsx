import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { IdentityPage } from "../../App";
import { adminUser } from "../../test/fixtures";

afterEach(() => vi.restoreAllMocks());

test("shows a newly issued secret once and clears it from browser state", async () => {
  const issued = {
    key: {
      id: "00000000-0000-4000-8000-000000000002",
      user_id: adminUser.id,
      name: "Console key",
      prefix: "coire_test",
      scopes: ["models:read"],
      requests_per_minute: 60,
      monthly_budget_tokens: 100,
      tokens_consumed: 0,
      active: true,
      created_at: adminUser.created_at,
      last_used_at: null,
      revoked_at: null,
    },
    secret: "coire_test_one_time_secret",
  };
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([adminUser]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(issued), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([issued.key]), { status: 200 })),
  );
  render(<IdentityPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Issue key" }));
  expect(await screen.findByText(issued.secret)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "I have stored it" }));
  expect(screen.queryByText(issued.secret)).not.toBeInTheDocument();
});
