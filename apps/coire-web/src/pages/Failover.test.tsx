import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { FailoverPage } from "./Failover";

const degraded = {
  tier: "degraded_inference" as const,
  elected_host: "coire-edge-a",
  in_flight: 0,
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
        in_flight: 0,
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
    new Response('data: {"choices":[{"delta":{"content":"hello"}}]}\n\n', {
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
