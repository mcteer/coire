import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { FailoverPage } from "./Failover";

const degraded = {
  tier: "degraded_inference" as const,
  elected_host: "coire-edge-a",
  admission_available: true,
  reason: "available",
  authority_expires_at: new Date(Date.now() + 120_000).toISOString(),
  snapshot_expires_at: new Date(Date.now() + 120_000).toISOString(),
  unavailable_capabilities: [
    "conversation_persistence",
    "admin",
    "model_acquisition",
    "training",
    "image_generation",
  ],
};

test("warns that nothing is persisted before the composer", () => {
  render(
    <FailoverPage
      status={degraded}
      models={[
        {
          id: "model-1",
          object: "model",
          owned_by: "coire",
          created: 1,
          coire_load_state: "loaded",
          coire_source: "studio",
          coire_description: "Tiny test model",
          coire_verified: false,
        },
      ]}
    />,
  );
  const warning = screen.getByRole("status");
  const composer = screen.getByLabelText("Message");
  expect(warning.textContent).toMatch(/nothing you send is persisted/i);
  expect(warning.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.getAllByText("Tiny test model")).toHaveLength(2);
  expect(screen.getByText("conversation persistence")).toBeTruthy();
  expect(screen.getByText("model acquisition")).toBeTruthy();
  expect(screen.queryByRole("link", { name: /admin/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /train/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /image/i })).toBeNull();
});

test("minimal tier says sharding is unavailable", () => {
  render(
    <FailoverPage
      status={{
        tier: "minimal",
        elected_host: "coire-edge-b",
        admission_available: false,
        reason: "not_elected",
        unavailable_capabilities: ["sharded_inference", "conversation_persistence"],
      }}
      models={[]}
    />,
  );
  expect(screen.getByText(/sharded inference is unavailable/i)).toBeTruthy();
  expect(screen.getByText("No resident model is available.")).toBeTruthy();
  expect(screen.getByText("sharded inference")).toBeTruthy();
});

test("sends an ephemeral message and displays the streamed answer", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response('data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n', {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
  );
  render(
    <FailoverPage
      status={degraded}
      models={[
        {
          id: "11111111-1111-1111-1111-111111111111",
          object: "model",
          owned_by: "coire",
          created: 1,
          coire_load_state: "loaded",
          coire_source: "studio",
          coire_verified: false,
        },
      ]}
    />,
  );
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: "hi" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText(/assistant:/i)).toBeTruthy());
  expect(screen.getByText("hello")).toBeTruthy();
  expect(fetch).toHaveBeenCalledWith(
    "/v1/chat/completions",
    expect.objectContaining({ method: "POST", credentials: "same-origin" }),
  );
  fetch.mockRestore();
});

const resident = {
  id: "11111111-1111-1111-1111-111111111111", object: "model" as const,
  owned_by: "coire" as const, created: 1, coire_load_state: "loaded" as const,
  coire_source: "studio" as const,
  coire_verified: false,
};

test("a lone fenced Studio is an outage, not working minimal service", () => {
  render(<FailoverPage status={{ ...degraded, tier: "minimal", elected_host: null, admission_available: false }} models={[resident]} />);
  expect(screen.getByRole("heading", { name: "Inference unavailable" })).toBeTruthy();
  expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
});

test("expired authority disables submission while preserving an unsent draft", () => {
  const { rerender } = render(<FailoverPage status={degraded} models={[resident]} />);
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: "keep this draft" } });
  rerender(<FailoverPage status={{ ...degraded, authority_expires_at: new Date(0).toISOString() }} models={[resident]} />);
  expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText("Message") as HTMLTextAreaElement).value).toBe("keep this draft");
  expect(screen.getByText(/authority expires/i)).toBeTruthy();
});

test("a refused request restores the unsent draft and reports no persistence", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(
    JSON.stringify({ reason: "request_limit_exceeded", status: 429 }),
    { status: 429, headers: { "Content-Type": "application/problem+json" } },
  ));
  render(<FailoverPage status={degraded} models={[resident]} />);
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: "retry this" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  expect((screen.getByLabelText("Message") as HTMLTextAreaElement).value).toBe("retry this");
  expect(screen.queryByText(/assistant:/i)).toBeNull();
  fetch.mockRestore();
});
