import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import {
  controlTraining,
  listTrainingActivity,
  type TrainingActivityItem,
} from "../../api/training";
import { TrainingActivity } from "./TrainingActivity";

vi.mock("../../api/training", () => ({ listTrainingActivity: vi.fn(), controlTraining: vi.fn() }));
const job: TrainingActivityItem = {
  job_id: "01ARZ3NDEKTSV4RRFFQ69G5FAV",
  owner_id: "00000000-0000-4000-8000-000000000001",
  model_id: "00000000-0000-4000-8000-000000000002",
  variant_id: "00000000-0000-4000-8000-000000000003",
  state: "recovering",
  completed_update: 4,
  total_updates: 100,
  reserved_bytes: 1024 ** 3,
  can_stop: true,
  safe_reason: "node_unreachable",
  version: 7,
  adapter_slug: "acceptance",
  started_at: "2026-10-05T00:00:00Z",
  latest_train_loss: 0.5,
  latest_validation_loss: 0.25,
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listTrainingActivity).mockResolvedValue({ items: [job], next_cursor: null });
});

it("shows retained reservation/loss history and uses the confirmed versioned stop lane", async () => {
  vi.mocked(controlTraining).mockResolvedValue({
    command_id: "00000000-0000-4000-8000-000000000004",
    job_id: job.job_id,
    state: "cancelling",
    version: 8,
  });
  render(<TrainingActivity />);
  expect(await screen.findByText("0.5000 / 0.2500")).toBeInTheDocument();
  expect(screen.getByText("1.00 GiB")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Stop acceptance" }));
  expect(controlTraining).not.toHaveBeenCalled();
  vi.mocked(listTrainingActivity).mockResolvedValue({
    items: [{ ...job, state: "cancelling", can_stop: false, version: 8 }],
    next_cursor: null,
  });
  fireEvent.click(screen.getByRole("button", { name: "Stop acceptance" }));
  await waitFor(() =>
    expect(controlTraining).toHaveBeenCalledWith(
      { id: job.job_id, version: 7 },
      "cancel",
      expect.any(String),
    ),
  );
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "Stop acceptance" })).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("link", { name: "acceptance" })).toHaveAttribute(
    "href",
    `#training/run/${job.job_id}`,
  );
});

it("keeps content-free history visible on a version conflict and loads older pages", async () => {
  vi.mocked(listTrainingActivity).mockResolvedValueOnce({
    items: [job],
    next_cursor: "opaque-cursor",
  });
  vi.mocked(controlTraining).mockRejectedValue(new Error("version conflict"));
  render(<TrainingActivity />);
  await screen.findByRole("link", { name: "acceptance" });
  fireEvent.click(screen.getByRole("button", { name: "Stop acceptance" }));
  fireEvent.click(screen.getByRole("button", { name: "Stop acceptance" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("version conflict");
  expect(screen.getByText("0.5000 / 0.2500")).toBeInTheDocument();
  vi.mocked(listTrainingActivity).mockResolvedValue({
    items: [
      {
        ...job,
        job_id: "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        adapter_slug: "older",
        state: "succeeded",
        can_stop: false,
      },
    ],
    next_cursor: null,
  });
  fireEvent.click(screen.getByRole("button", { name: "Load older training jobs" }));
  expect(await screen.findByRole("link", { name: "older" })).toBeInTheDocument();
  expect(listTrainingActivity).toHaveBeenLastCalledWith("opaque-cursor");
});
