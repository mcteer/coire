import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useChatTurnStream } from "./useEventStream";
import type { ChatTurnCreate } from "../api/chat";

const mockSendChatTurn = vi.fn();
vi.mock("../api/chat", () => ({ sendChatTurn: (...args: unknown[]) => mockSendChatTurn(...args) }));
afterEach(() => vi.clearAllMocks());

const body: ChatTurnCreate = {
  client_request_id: "00000000-0000-0000-0000-000000000002",
  expected_revision: 1,
  model_id: "00000000-0000-0000-0000-000000000003",
  content: "Hi",
  action: "chat",
};
let controls: ReturnType<typeof useChatTurnStream>;
function Probe() {
  controls = useChatTurnStream();
  return <output>{controls.active ? "active" : "idle"}</output>;
}

test("keeps a hidden generation connected and aborts on unmount", async () => {
  let signal: AbortSignal | undefined;
  mockSendChatTurn.mockImplementation(
    (_id: string, _body: ChatTurnCreate, incoming: AbortSignal) =>
      new Promise<void>((_resolve, reject) => {
        signal = incoming;
        incoming.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          {
            once: true,
          },
        );
      }),
  );
  const view = render(<Probe />);
  let running: Promise<void> | undefined;
  act(() => {
    running = controls.send("conversation", body, () => {});
  });
  await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  expect(signal?.aborted).toBe(false);
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await expect(running).rejects.toMatchObject({ name: "AbortError" });
  expect(mockSendChatTurn).toHaveBeenCalledTimes(1);
});
