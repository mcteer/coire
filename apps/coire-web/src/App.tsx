import { useEffect, useState } from "react";

type ServiceHealth = {
  name: string;
  healthy: boolean;
  detail: string | null;
  checked_at: string;
  latency_ms: number | null;
};

type HealthResponse = {
  status: "healthy" | "degraded" | "unhealthy";
  version: string;
  services: ServiceHealth[];
  nodes: ServiceHealth[];
  generated_at: string;
};

const STATUS_COLOUR: Record<HealthResponse["status"], string> = {
  healthy: "#1a7f37",
  degraded: "#9a6700",
  unhealthy: "#cf222e",
};

function Row({ s }: { s: ServiceHealth }) {
  return (
    <tr>
      <td>{s.name}</td>
      <td style={{ color: s.healthy ? STATUS_COLOUR.healthy : STATUS_COLOUR.unhealthy }}>
        {s.healthy ? "healthy" : "unhealthy"}
      </td>
      <td>{s.latency_ms === null ? "—" : `${s.latency_ms.toFixed(0)} ms`}</td>
      <td>{s.detail ?? ""}</td>
    </tr>
  );
}

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const resp = await fetch("/health");
        const body = (await resp.json()) as HealthResponse;
        if (!cancelled) {
          setHealth(body);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    };
    void load();
    const timer = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", margin: "2rem", maxWidth: "48rem" }}>
      <h1 style={{ marginBottom: 0 }}>Coire</h1>
      <p style={{ color: "#57606a", marginTop: "0.25rem" }}>
        Control plane · feature 000 bootstrap
      </p>

      {error && <p style={{ color: STATUS_COLOUR.unhealthy }}>cannot reach /health: {error}</p>}

      {health && (
        <>
          <p>
            <strong style={{ color: STATUS_COLOUR[health.status] }}>{health.status}</strong>
            <span style={{ color: "#57606a" }}> · v{health.version}</span>
          </p>

          <h2 style={{ fontSize: "1rem" }}>Services</h2>
          <table cellPadding={6} style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid #d0d7de" }}>
                <th>name</th>
                <th>state</th>
                <th>latency</th>
                <th>detail</th>
              </tr>
            </thead>
            <tbody>
              {health.services.map((s) => (
                <Row key={s.name} s={s} />
              ))}
            </tbody>
          </table>

          <h2 style={{ fontSize: "1rem" }}>Nodes</h2>
          {health.nodes.length === 0 ? (
            <p style={{ color: "#57606a" }}>No nodes registered yet.</p>
          ) : (
            <table cellPadding={6} style={{ borderCollapse: "collapse", width: "100%" }}>
              <tbody>
                {health.nodes.map((n) => (
                  <Row key={n.name} s={n} />
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </main>
  );
}
