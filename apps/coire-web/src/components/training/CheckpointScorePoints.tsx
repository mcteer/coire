import type { EvaluationGroup, EvaluationGroupLink } from "../../api/evaluations";

export function CheckpointScorePoints({ group, link, maximumUpdate }: { group: EvaluationGroup; link: EvaluationGroupLink; maximumUpdate: number }) {
  const points = (group.runs ?? []).flatMap((run) => run.state === "succeeded" && run.result?.outcome === "succeeded" && run.suite.template.mode !== "pairwise" ? run.result.aggregates.flatMap((score, index) => score !== null && Number.isFinite(score) ? [{ key: `${run.id}:${index}`, score, subject: run.result!.subjects[index]?.public_selector, suite: run.suite.suite_id }] : []) : []);
  if (!points.length || link.completed_update == null) return null;
  return <section aria-label="Completed checkpoint scores">
    <svg viewBox="0 0 600 210" role="img" aria-label={`Checkpoint scores at completed update ${link.completed_update}, attempt ${link.attempt_id ?? "unavailable"}, fence ${link.fence ?? "unavailable"}`}>
      <path d="M30 10V180H580" className="training-axis"/>
      {points.map((point) => <circle key={point.key} cx={30 + link.completed_update! / Math.max(1, maximumUpdate) * 550} cy={180 - point.score * 160} r={4} fill="currentColor"><title>{point.suite} · {point.subject} · update {link.completed_update} · attempt {link.attempt_id} · score {point.score}</title></circle>)}
      <text x={5} y={20}>1</text><text x={5} y={180}>0</text><text x={30} y={202}>0</text><text x={500} y={202}>{maximumUpdate} updates</text>
    </svg>
    <p>Independent checkpoint score points; no line joins attempts or recovered updates.</p>
    <table aria-label="Checkpoint score point values"><thead><tr><th>Attempt</th><th>Update</th><th>Suite</th><th>Subject</th><th>Score</th></tr></thead><tbody>{points.map((point) => <tr key={point.key}><td>{link.attempt_id ?? "Unavailable"}</td><td>{link.completed_update}</td><td>{point.suite}</td><td>{point.subject}</td><td>{point.score.toFixed(3)}</td></tr>)}</tbody></table>
  </section>;
}
