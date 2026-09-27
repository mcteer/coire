import type { ConsoleSnapshot, User } from "../api/client";

export const adminUser: User = {
  id: "00000000-0000-4000-8000-000000000001",
  email: "admin@example.test",
  display_name: "Admin",
  role: "admin",
  active: true,
  entitlements: [],
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

export function consoleSnapshot(overrides: Partial<ConsoleSnapshot> = {}): ConsoleSnapshot {
  const observedAt = "2026-09-01T00:00:00Z";
  return {
    observed_at: observedAt,
    cursor: "1",
    capabilities: {
      cluster: true,
      models: true,
      instances: true,
      jobs: true,
      identity: true,
      audit: true,
      ask: true,
    },
    cluster: { observed_at: observedAt, nodes: [], instances: [], studio_link: null },
    ledgers: [],
    alerts: [],
    ...overrides,
  };
}
