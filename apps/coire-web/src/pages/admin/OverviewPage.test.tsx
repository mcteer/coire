import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { Overview } from "../../App";
import type { ConsoleSnapshot } from "../../api/client";
import { consoleSnapshot } from "../../test/fixtures";

test("distinguishes degraded, unreachable, and stale node state", () => {
  const at = "2026-09-01T00:00:00Z";
  const ids = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"];
  const snapshot = consoleSnapshot({
    cluster: {
      observed_at: at,
      instances: [],
      studio_link: null,
      nodes: ids.map((id, index) => ({
        id,
        name: `coire-edge-${index ? "b" : "a"}`,
        reachability: index ? "unreachable" : "degraded",
        health_observed_at: at,
        cpu_percent: null,
        gpu_percent: null,
        thermal_state: "unknown",
        health_reason: "probe failed",
        stale: index === 0,
        memory_total_bytes: 100,
        memory_free_bytes: 50,
        disk_total_bytes: 100,
        disk_free_bytes: 50,
        budget_bytes: 90,
        reserved_bytes: 10,
        reservations: [],
      })),
    },
    ledgers: ids.map((node_id, index) => ({
      node_id,
      node_name: `coire-edge-${index ? "b" : "a"}`,
      budget_bytes: 90,
      sandbox_bytes: 0,
      reserved_bytes: 10,
      free_bytes: 80,
      measured_resident_bytes: null,
      drift_ratio: null,
      health: index ? "unreachable" : "degraded",
      health_reason: "probe failed",
      health_sampled_at: at,
      reservations: [],
      updated_at: at,
    })),
  } as Partial<ConsoleSnapshot>);
  render(<Overview snapshot={snapshot} />);
  expect(screen.getByText("degraded · stale")).toBeInTheDocument();
  expect(screen.getByText("unreachable")).toBeInTheDocument();
  expect(screen.getAllByText("probe failed")).toHaveLength(2);
});
