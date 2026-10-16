import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { ImageTimeline } from "./ImageTimeline";

const JOB = "01J00000000000000000000000";

test("shows queue and progress and stops only before a terminal server state", () => {
  const stop = vi.fn();
  const { rerender } = render(
    <ImageTimeline
      jobId={JOB}
      state="queued"
      step={null}
      totalSteps={null}
      onStop={stop}
      busy={false}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("Queued");
  expect(screen.getByText("Prompt cache status unavailable for this job.")).toBeInTheDocument();
  expect(screen.getByText("Worker residency status unavailable for this job.")).toBeInTheDocument();
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: `Stop image job ${JOB}` }));
  expect(stop).toHaveBeenCalledOnce();

  rerender(
    <ImageTimeline
      jobId={JOB}
      state="running"
      step={2}
      totalSteps={8}
      onStop={stop}
      busy={false}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("step 2 of 8");
  expect(screen.getByRole("progressbar", { name: "Image generation progress" })).toHaveAttribute(
    "value",
    "2",
  );

  rerender(
    <ImageTimeline
      jobId={JOB}
      state="succeeded"
      step={8}
      totalSteps={8}
      onStop={stop}
      busy={false}
    />,
  );
  expect(screen.queryByRole("button", { name: `Stop image job ${JOB}` })).not.toBeInTheDocument();
});

test("reports measured cache reuse, eviction, and observed worker residency", () => {
  const { rerender } = render(
    <ImageTimeline
      jobId={JOB}
      state="running"
      step={1}
      totalSteps={2}
      cacheStatus="hit"
      workerResidency="resident"
      onStop={vi.fn()}
      busy={false}
    />,
  );
  expect(screen.getByText("Prompt cache reused.")).toBeInTheDocument();
  expect(
    screen.getByText("Worker observed resident at the last progress update."),
  ).toBeInTheDocument();
  rerender(
    <ImageTimeline
      jobId={JOB}
      state="running"
      step={2}
      totalSteps={2}
      cacheStatus="evicted"
      onStop={vi.fn()}
      busy={false}
    />,
  );
  expect(screen.getByText("Prompt cache evicted: encoding ran again.")).toBeInTheDocument();
  expect(screen.getByText("Worker residency status unavailable for this job.")).toBeInTheDocument();
});

test("disables stop while cancellation is in flight", () => {
  render(
    <ImageTimeline jobId={JOB} state="running" step={1} totalSteps={4} onStop={vi.fn()} busy />,
  );
  expect(screen.getByRole("button", { name: `Stop image job ${JOB}` })).toBeDisabled();
  expect(screen.getByRole("button", { name: `Stop image job ${JOB}` })).toHaveTextContent(
    "Stopping…",
  );
});

test("explains queue, preparation, transfer and completion with a result action", () => {
  const view = vi.fn();
  const props = {
    jobId: JOB,
    step: null,
    totalSteps: null,
    busy: false,
    onStop: vi.fn(),
    onViewResult: view,
  };
  const { rerender } = render(<ImageTimeline {...props} state="queued" queuePosition={2} />);
  expect(screen.getByRole("status")).toHaveTextContent("Queued");
  expect(screen.getByText(/Position 2/)).toBeInTheDocument();
  rerender(<ImageTimeline {...props} state="reserving" />);
  expect(screen.getByRole("status")).toHaveTextContent("Preparing generation");
  rerender(<ImageTimeline {...props} state="transferring" />);
  expect(screen.getByRole("status")).toHaveTextContent("Saving your image");
  rerender(<ImageTimeline {...props} state="succeeded" />);
  expect(screen.getByRole("status")).toHaveTextContent("Image ready");
  fireEvent.click(screen.getByRole("button", { name: "View in library" }));
  expect(view).toHaveBeenCalledOnce();
});

test("does not invent a percentage without a measured total and explains reconnecting", () => {
  render(
    <ImageTimeline
      jobId={JOB}
      state="running"
      step={null}
      totalSteps={null}
      onStop={vi.fn()}
      busy={false}
      error="Error: stream ended"
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("Generating your image");
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  expect(screen.getByText(/Reconnecting to live progress/)).toBeInTheDocument();
});

test.each(["failed", "cancelled"] as const)(
  "makes %s terminal with no misleading result or stop action",
  (state) => {
    render(
      <ImageTimeline
        jobId={JOB}
        state={state}
        step={null}
        totalSteps={null}
        busy={false}
        onStop={vi.fn()}
        onViewResult={vi.fn()}
        error={state === "failed" ? "worker_unavailable" : null}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      state === "failed" ? "Generation failed" : "Generation stopped",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    if (state === "failed")
      expect(screen.getByRole("alert")).toHaveTextContent("worker_unavailable");
  },
);
