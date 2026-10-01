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
  expect(screen.getByRole("status")).toHaveTextContent("queued");
  expect(screen.getByText("Cache status unavailable for this job.")).toBeInTheDocument();
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

test("disables stop while cancellation is in flight", () => {
  render(
    <ImageTimeline jobId={JOB} state="running" step={1} totalSteps={4} onStop={vi.fn()} busy />,
  );
  expect(screen.getByRole("button", { name: `Stop image job ${JOB}` })).toBeDisabled();
  expect(screen.getByRole("button", { name: `Stop image job ${JOB}` })).toHaveTextContent(
    "Stopping…",
  );
});
