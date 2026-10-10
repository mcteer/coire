import { EvaluationGroups } from "../evaluations/EvaluationGroups";
import { CheckpointEvaluations } from "./CheckpointEvaluations";
import { useTrainingJob } from "../../hooks/useTrainingJob";
import { TrainingControls } from "./TrainingControls";
import { Checkpoints } from "./Checkpoints";
import type { TrainingMetric } from "../../api/training";

export function TrainingLoss({ metrics }: { metrics: TrainingMetric[] }) {
  const groups = new Map<string, TrainingMetric[]>();
  for (const metric of metrics) {
    const key = `${metric.attempt_id}:${metric.kind}:${metric.rolled_back ? "rolled back" : "retained"}`;
    groups.set(key, [...(groups.get(key) ?? []), metric]);
  }
  const finite = metrics.filter((m) => Number.isFinite(m.loss));
  const maximum = Math.max(1, ...finite.map((m) => m.update));
  const low = Math.min(0, ...finite.map((m) => m.loss)),
    high = Math.max(1, ...finite.map((m) => m.loss));
  return (
    <section aria-label="Persisted loss history" className="training-card">
      <h3>Training and held-out loss</h3>
      {!metrics.length ? (
        <p>No persisted loss samples yet.</p>
      ) : (
        <>
          <svg
            viewBox="0 0 600 210"
            role="img"
            aria-label="Loss curves separated by attempt, kind and rolled-back state"
          >
            <path d="M30 10V180H580" className="training-axis" />
            {[...groups].map(([key, samples]) => (
              <g
                key={key}
                className={
                  samples[0].kind === "validation" ? "training-validation" : "training-train"
                }
                opacity={samples[0].rolled_back ? 0.45 : 1}
              >
                <polyline
                  fill="none"
                  strokeWidth={2}
                  strokeDasharray={
                    samples[0].rolled_back || samples[0].kind === "validation" ? "4 4" : undefined
                  }
                  points={samples
                    .filter((m) => Number.isFinite(m.loss))
                    .map(
                      (m) =>
                        `${30 + (m.update / maximum) * 550},${180 - ((m.loss - low) / (high - low)) * 160}`,
                    )
                    .join(" ")}
                />
                {samples
                  .filter((m) => Number.isFinite(m.loss))
                  .map((m) => (
                    <circle
                      key={m.update}
                      cx={30 + (m.update / maximum) * 550}
                      cy={180 - ((m.loss - low) / (high - low)) * 160}
                      r={3}
                    >
                      <title>
                        Attempt {m.attempt_id} · update {m.update} · {m.kind} loss {m.loss}
                        {m.rolled_back ? " · rolled back" : ""}
                      </title>
                    </circle>
                  ))}
              </g>
            ))}
            <text x={30} y={202}>
              0
            </text>
            <text x={530} y={202}>
              {maximum} updates
            </text>
          </svg>
          <p>
            Lines never join different attempts. Dashed muted segments are rolled back; held-out
            loss is shown separately.
          </p>
          <details>
            <summary>Accessible loss samples and attempt boundaries ({metrics.length})</summary>
            <table>
              <thead>
                <tr>
                  <th>Attempt</th>
                  <th>Update</th>
                  <th>Kind</th>
                  <th>Loss</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {metrics.map((m) => (
                  <tr key={`${m.attempt_id}:${m.update}:${m.kind}`}>
                    <td className="mono">{m.attempt_id}</td>
                    <td>{m.update}</td>
                    <td>{m.kind}</td>
                    <td>{m.loss}</td>
                    <td>{m.rolled_back ? "Rolled back" : "Retained"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </section>
  );
}
export function TrainingRun({ id }: { id: string }) {
  const { job, metrics, checkpoints, error, connected, streamError, refresh } = useTrainingJob(id);
  if (!job)
    return (
      <section>
        {error ? (
          <p role="alert" className="error">
            {error}
          </p>
        ) : (
          <p role="status">Loading training run…</p>
        )}
      </section>
    );
  const latest = metrics
    .filter((m) => m.kind === "train" && !m.rolled_back && m.attempt_id === job.attempt_id)
    .at(-1);
  const envelope = job.resolved?.resource_envelope;
  const memory = envelope
    ? envelope.weight_bytes +
      envelope.adapter_bytes +
      envelope.optimizer_bytes +
      envelope.activation_bytes +
      envelope.buffer_bytes +
      envelope.safety_bytes +
      ("reference_weight_bytes" in envelope
        ? envelope.reference_weight_bytes + envelope.reference_adapter_bytes
        : 0)
    : null;
  return (
    <section aria-label="Selected training run">
      <div className="row">
        <h1>{job.spec.output.adapter_slug}</h1>
        <span className="status">{job.state}</span>
      </div>
      <p className="mono">
        {job.id} · attempt {job.attempt_id ?? "not started"}
      </p>
      <p role="status">
        {connected
          ? "Live progress connected"
          : "Live progress disconnected; refreshing authoritative history every 5 s. Browser disconnect does not cancel training."}
      </p>
      {streamError && <p className="muted">{streamError}</p>}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {job.reason && (
        <p className={job.reason === "impossible_fit" ? "error" : "muted"}>
          Reason: {job.reason.replaceAll("_", " ")}
          {job.reason === "profile_missing"
            ? " — unmeasured; no workload may start without evidence."
            : job.reason === "impossible_fit"
              ? " — cannot fit even after eligible eviction."
              : job.reason === "capacity_busy"
                ? " — temporary contention; queued with bounded waiting."
                : ""}
        </p>
      )}
      <TrainingControls job={job} onChange={refresh} />
      <div className="training-spec-grid">
        <div className="training-card">
          <h3>Base variant</h3>
          <p className="mono">
            {job.spec.model.model_id}
            <br />
            {job.spec.model.variant_id}
          </p>
        </div>
        <div className="training-card">
          <h3>Objective / parameters</h3>
          <p>
            {job.spec.objective.toUpperCase()} · {job.spec.parameterization.kind} · rank{" "}
            {job.spec.parameterization.rank}
          </p>
        </div>
        <div className="training-card">
          <h3>Data</h3>
          <p>
            {job.spec.data.train.datasets.length} immutable sources ·{" "}
            {job.spec.data.train.mixture_strategy}
          </p>
        </div>
        <div className="training-card">
          <h3>Optimizer</h3>
          <p>
            {job.spec.optim.name} · {job.spec.optim.learning_rate} ·{" "}
            {job.spec.optim.schedule?.kind ?? "constant"}
          </p>
        </div>
      </div>
      <p className="mono">
        Update {job.completed_update} / {job.spec.optim.updates}
      </p>
      <progress
        max={job.spec.optim.updates}
        value={job.completed_update}
        aria-label="Completed optimizer updates"
      />
      <p className="mono">
        {latest
          ? `Loss ${latest.loss} · lr ${latest.learning_rate} · ${latest.tokens_per_second} tokens/s · ${latest.updates_per_second} updates/s · footprint ${(latest.footprint_bytes / 1024 ** 3).toFixed(2)} GiB`
          : "Loss, learning rate, throughput and footprint unavailable until persisted progress arrives."}
      </p>
      <p className="mono">
        Measured memory envelope:{" "}
        {memory === null ? "Unavailable" : `${(memory / 1024 ** 3).toFixed(2)} GiB per Studio`}
      </p>
      {!job.reproducible && (
        <p className="error">
          Inputs purged or unavailable: this historical run is no longer reproducible.
        </p>
      )}
      <TrainingLoss metrics={metrics} />
      <PreferenceProbes metrics={metrics} />
      <Checkpoints job={job} checkpoints={checkpoints} onChange={refresh} />
      <CheckpointEvaluations
        currentCheckpoint={job.latest_checkpoint_id}
        links={job.evaluation_groups ?? []}
      />
      <EvaluationGroups
        links={(job.evaluation_groups ?? []).filter(
          (link) => link.origin !== "training_checkpoint",
        )}
      />
      <details>
        <summary>Immutable original YAML · {job.source_sha256}</summary>
        <pre className="mono">{job.source_yaml}</pre>
      </details>
      <details>
        <summary>Immutable resolved settings / runtime / input identities</summary>
        {job.resolved ? (
          <pre className="mono">{JSON.stringify(job.resolved, null, 2)}</pre>
        ) : (
          <p>Resolution pending; runtime and measured capacity are unavailable.</p>
        )}
      </details>
    </section>
  );
}

export function PreferenceProbes({ metrics }: { metrics: TrainingMetric[] }) {
  const pairs = metrics.filter((metric) => "objective" in metric);
  if (!pairs.length) return null;
  return (
    <section aria-label="Preference objective history">
      <h3>Preference objective and post-update probes</h3>
      <p>
        Loss is a mean per pair. Probes use up to eight held-out pairs after the update and are
        separate from training loss.
      </p>
      <table>
        <thead>
          <tr>
            <th>Attempt / update</th>
            <th>Objective</th>
            <th>Pairs / response tokens</th>
            <th>Probe accuracy / margin</th>
            <th>Chosen NLL / odds penalty</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {pairs.map(
            (metric) =>
              "objective" in metric && (
                <tr key={`${metric.attempt_id}:${metric.update}:${metric.kind}`}>
                  <td>
                    {metric.attempt_id} / {metric.update}
                  </td>
                  <td>{metric.objective.toUpperCase()} · pair mean</td>
                  <td>
                    {metric.pair_count} / {metric.response_tokens}
                  </td>
                  <td>
                    {metric.probe
                      ? `${metric.probe.accuracy} / ${metric.probe.margin} (${metric.probe.sample_count} held-out pairs)`
                      : "No post-update probe"}
                  </td>
                  <td>
                    {metric.probe?.chosen_nll ?? "—"} / {metric.probe?.odds_penalty ?? "—"}
                  </td>
                  <td>{metric.rolled_back ? "Rolled back" : "Retained"}</td>
                </tr>
              ),
          )}
        </tbody>
      </table>
    </section>
  );
}
