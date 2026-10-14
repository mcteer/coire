import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import * as client from "../api/client";
import { useEventStream } from "../hooks/useEventStream";
import { upgradeOperation, upgradeReleases, upgradeTargets } from "../test/upgradeFixtures";
import { UpgradesPage } from "./UpgradesPage";

vi.mock("../hooks/useEventStream", () => ({
  useEventStream: vi.fn(() => ({ data: null, connected: true, error: null })),
}));
beforeEach(() => {
  vi.mocked(useEventStream).mockReturnValue({ data: null, connected: true, error: null });
  window.location.hash = "admin/upgrades";
  sessionStorage.clear();
  vi.spyOn(client, "getUpgradeTargets").mockResolvedValue(upgradeTargets);
  vi.spyOn(client, "listUpgradeReleases").mockResolvedValue({
    items: upgradeReleases,
    next_cursor: null,
  });
  vi.spyOn(client, "getUpgradeOperation").mockResolvedValue(upgradeOperation);
});
afterEach(() => vi.restoreAllMocks());

test("acknowledged selection decision disables cancellation while finalization continues", async () => {
  vi.mocked(client.getUpgradeOperation).mockResolvedValue({
    ...upgradeOperation,
    phase: "committing",
    cancellation_allowed: false,
  });
  render(<UpgradesPage enabled initialJobId={upgradeOperation.job_id} />);
  expect(await screen.findByText(/cancellation is no longer available/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Request cancellation" })).toBeDisabled();
});

test("disabled upgrade capability requests no privileged data", () => {
  render(<UpgradesPage enabled={false} />);
  expect(screen.getByRole("heading", { name: "Upgrades unavailable" })).toBeInTheDocument();
  expect(client.getUpgradeTargets).not.toHaveBeenCalled();
  expect(client.listUpgradeReleases).not.toHaveBeenCalled();
});

test("unready recovery and source blockers remain visible and disable activation", async () => {
  vi.mocked(client.getUpgradeTargets).mockResolvedValue({
    items: [
      {
        ...upgradeTargets.items[0],
        readiness: "not_ready",
        bootstrap_ready: false,
        blocking_reasons: ["source_manifest_missing"],
      },
    ],
  });
  render(<UpgradesPage enabled />);
  await screen.findByText("source manifest missing");
  expect(screen.getByText(/Recovery bootstrap not ready/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start upgrade" })).toBeDisabled();
});

test("maintenance acknowledgment and exact candidate are required; repeated clicks admit one request", async () => {
  let complete: (operation: client.UpgradeOperation) => void = () => {};
  const start = vi.spyOn(client, "startStudioUpgrade").mockImplementation(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  render(<UpgradesPage enabled />);
  await screen.findByRole("option", { name: /target ·/ });
  const button = screen.getByRole("button", { name: "Start upgrade" });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Candidate release"), {
    target: { value: upgradeReleases[1].release_id },
  });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button);
  fireEvent.click(button);
  expect(start).toHaveBeenCalledTimes(1);
  expect(start).toHaveBeenCalledWith(
    "coire-edge-a",
    expect.objectContaining({
      release_id: upgradeReleases[1].release_id,
      expected_source_sha256: upgradeTargets.items[0].observed_sha256,
      acknowledge_maintenance: true,
    }),
  );
  complete(upgradeOperation);
  await screen.findByRole("region", { name: "Upgrade progress" });
  expect(window.location.hash).toContain(upgradeOperation.job_id);
});

test("lost start response retries the original request identity", async () => {
  const start = vi
    .spyOn(client, "startStudioUpgrade")
    .mockRejectedValueOnce(new Error("API offline"))
    .mockResolvedValueOnce(upgradeOperation);
  const view = render(<UpgradesPage enabled />);
  await screen.findByRole("option", { name: /target ·/ });
  fireEvent.change(screen.getByLabelText("Candidate release"), {
    target: { value: upgradeReleases[1].release_id },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Start upgrade" }));
  await screen.findByRole("alert");
  view.unmount();
  vi.mocked(client.getUpgradeTargets).mockResolvedValue({
    items: [{ ...upgradeTargets.items[0], readiness: "maintenance" }],
  });
  render(<UpgradesPage enabled />);
  fireEvent.click(screen.getByRole("button", { name: "Retry previous request" }));
  await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
  expect(start.mock.calls[0][1].idempotency_key).toBe(start.mock.calls[1][1].idempotency_key);
  await waitFor(() => expect(sessionStorage.getItem("coire.upgrades.pending")).toBeNull());
});

test("resumed rolled-back operation stays distinct from success and restores focus", async () => {
  vi.mocked(client.getUpgradeOperation).mockResolvedValue({
    ...upgradeOperation,
    phase: "rolled_back",
    finished_at: new Date().toISOString(),
  });
  render(<UpgradesPage enabled initialJobId={upgradeOperation.job_id} />);
  expect(await screen.findByText("Rolled back")).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Upgrade progress" })).toHaveFocus();
  expect(screen.getByRole("button", { name: "Request cancellation" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Roll back to retained source" })).toBeDisabled();
});

test("recovery-required snapshot retains maintenance and cancellation uncertainty", async () => {
  vi.mocked(useEventStream).mockReturnValue({
    data: {
      operation: { ...upgradeOperation, phase: "recovery_required", cancel_requested: true },
      events: [],
      cursor: 1,
      reset: false,
    },
    connected: false,
    error: "API offline",
  });
  render(<UpgradesPage enabled initialJobId={upgradeOperation.job_id} />);
  expect(await screen.findByText(/Maintenance remains in place/)).toBeInTheDocument();
  expect(screen.getByText(/Waiting for authoritative cleanup/)).toBeInTheDocument();
  expect(screen.getByText(/Reconnecting to this operation/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Request cancellation" })).toBeDisabled();
});

test("revoked progress access disables mutations and reports that reconnection stopped", async () => {
  vi.mocked(useEventStream).mockReturnValue({
    data: null,
    connected: false,
    error: "Error: stream refused (403)",
    accessDenied: true,
  });
  const cancel = vi.spyOn(client, "cancelUpgrade");
  render(<UpgradesPage enabled initialJobId={upgradeOperation.job_id} />);
  expect(await screen.findByText(/Upgrade access is no longer available/)).toBeInTheDocument();
  const button = screen.getByRole("button", { name: "Request cancellation" });
  expect(button).toBeDisabled();
  fireEvent.click(button);
  expect(cancel).not.toHaveBeenCalled();
  expect(screen.queryByText(/Progress disconnected/)).not.toBeInTheDocument();
});

test("cancellation waits for authoritative restoration before showing rolled back", async () => {
  vi.spyOn(client, "cancelUpgrade").mockResolvedValue({
    ...upgradeOperation,
    phase: "restoring",
    cancel_requested: true,
  });
  const view = render(<UpgradesPage enabled initialJobId={upgradeOperation.job_id} />);
  fireEvent.click(await screen.findByRole("button", { name: "Request cancellation" }));
  expect(
    await screen.findByText(/Cancellation requested. Waiting for authoritative cleanup/),
  ).toBeInTheDocument();
  expect(screen.queryByText("Rolled back")).not.toBeInTheDocument();
  vi.mocked(useEventStream).mockReturnValue({
    data: {
      operation: { ...upgradeOperation, phase: "rolled_back", finished_at: "2026-10-11T01:00:00Z" },
      events: [],
      cursor: 0,
      reset: false,
    },
    connected: true,
    error: null,
  });
  view.rerender(<UpgradesPage enabled initialJobId={upgradeOperation.job_id} />);
  expect(await screen.findByText("Rolled back")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Request cancellation" })).toBeDisabled();
  expect(client.cancelUpgrade).toHaveBeenCalledTimes(1);
});
