import { useEffect, useState } from "react";
import { getTrainingMeasurement, listTrainingProfiles, submitTrainingMeasurement, type TrainingMeasurementRequest, type TrainingMeasurementResult, type TrainingProfile, type TrainingValidation } from "../../api/training";
/** Missing evidence is a diagnostic, never a positive capability assertion. */
export function TrainingMeasurements({ validation }: { validation?: TrainingValidation | null }) {
  const [profiles, setProfiles] = useState<TrainingProfile[] | null>(null), [error, setError] = useState(""), [requestText, setRequestText] = useState(""), [busy, setBusy] = useState(false), [measurementId, setMeasurementId] = useState(""), [result, setResult] = useState<TrainingMeasurementResult | null>(null);
  useEffect(() => { let live = true; listTrainingProfiles().then((page) => { if (live) setProfiles(page.items); }).catch((e) => { if (live) setError(`Measurement and profile capability unavailable: ${String(e)}`); }); return () => { live = false; }; }, []);
  useEffect(() => {
    if (!measurementId) return;
    let live = true;
    const refresh = () => void getTrainingMeasurement(measurementId).then((next) => { if (live) setResult(next); }).catch((e) => { if (live) setError(String(e)); });
    refresh(); const timer = window.setInterval(refresh, 5000); return () => { live = false; window.clearInterval(timer); };
  }, [measurementId]);
  return <section className="training-card" aria-label="Measured training capabilities">
    <h3>Measured capabilities and chat protection</h3>
    <p>{validation?.resolved ? "A measured resource envelope is present in this resolved preview. Admission still rechecks current node and protected workload state." : "Training memory evidence unavailable. Weight size alone does not authorize a training reservation."}</p>
    {validation?.reasons?.map((reason) => <p key={reason} className={reason === "impossible_fit" ? "error" : "muted"}>{reason.replaceAll("_", " ")}{reason === "impossible_fit" ? ": full per-node memory cannot fit even after eligible eviction." : reason === "profile_expired" ? ": remeasure the exact runtime, recipe and resident targets." : reason === "profile_missing" ? ": unmeasured combination; do not start speculatively." : reason === "insufficient_samples" ? ": missing traffic is not a passing latency result." : ""}</p>)}
    <p>Two-Studio and same-Studio chat coexistence are unavailable until their exact capability evidence passes. Pinned models and active requests stay protected; image generation and training are mutually exclusive per node.</p>
    <p>Coexistence requires identical frozen 15-minute baseline and mixed workloads, ≥100 completions per target in each phase, p95 first-token latency ≤1.5 s, positive training progress and zero swap growth. Live guards require ≥30 samples per target over 5 minutes and telemetry age ≤60 s.</p>
    {error && <p role="status">{error}</p>}
    {profiles === null && !error && <p role="status">Loading measured profiles…</p>}
    {profiles?.length === 0 && <p>No measured profiles. Unmeasured combinations cannot authorize admission.</p>}
    {profiles?.map((profile) => <article className="training-card" key={profile.id}><h4>{profile.id}</h4><p>{profile.invalidated_reason ? `Invalidated: ${profile.invalidated_reason.replaceAll("_", " ")}` : Date.parse(profile.expires_at) <= Date.now() ? "Expired — remeasurement required" : "Current measured evidence — exact identities and bounds must still match"}</p><p className="mono">Expires {profile.expires_at} · report {profile.report_sha256}</p><details><summary>Frozen measured configuration</summary><pre className="mono">{JSON.stringify(profile.request, null, 2)}</pre></details></article>)}
    <form className="training-fields" onSubmit={(event) => {
      event.preventDefault(); if (profiles === null) return; setBusy(true); setError("");
      let request: TrainingMeasurementRequest;
      try { request = JSON.parse(requestText) as TrainingMeasurementRequest; if (!request || typeof request !== "object" || !["memory", "coexistence"].includes(request.mode)) throw new Error("A typed memory or coexistence measurement request is required."); }
      catch (e) { setError(String(e)); setBusy(false); return; }
      void submitTrainingMeasurement(request, crypto.randomUUID()).then((receipt) => setMeasurementId(receipt.measurement_id)).catch((e) => setError(String(e))).finally(() => setBusy(false));
    }}>
      <label>Schema-backed measurement request (JSON)<textarea rows={8} required value={requestText} onChange={(e) => setRequestText(e.target.value)} placeholder="Provide a fully bound TrainingSpec, declared nodes, exact resident targets and frozen workload identity"/></label>
      <p>The backend validates the generated TrainingMeasurementRequest contract, reserves the experiment and enforces watchdogs. A caller-provided pass flag is never accepted.</p>
      <button className="button ghost" disabled={busy || profiles === null || !requestText.trim()}>Submit guarded measurement</button>
    </form>
    <label>Measurement ID<input value={measurementId} onChange={(e) => { setMeasurementId(e.target.value); setResult(null); }}/></label>
    {result && <article aria-label="Measurement result"><h4>{result.state}</h4>
      <p>Updates {result.completed_updates} · swap growth {result.swap_growth_bytes} bytes · thermal {result.thermal_ok ? "passed" : "not confirmed"}</p>
      <p>Approved profile: {result.profile_id ?? "Unavailable — failed, pending or inconclusive evidence does not approve a profile"}</p>
      <table><thead><tr><th>Exact instance</th><th>Baseline requests / p95</th><th>Mixed requests / p95</th></tr></thead><tbody>{result.targets?.map((target) => <tr key={target.instance_id}><td className="mono">{target.instance_id}</td><td>{target.baseline_requests} / {target.baseline_p95_seconds} s</td><td>{target.mixed_requests} / {target.mixed_p95_seconds} s</td></tr>)}</tbody></table>
      <details><summary>Immutable request and report</summary><pre className="mono">{JSON.stringify(result, null, 2)}</pre></details>
    </article>}
  </section>;
}
