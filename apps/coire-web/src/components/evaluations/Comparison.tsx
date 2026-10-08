import type { EvaluationComparison } from "../../api/evaluations";
import { Preferences } from "./Preferences";
export function Comparison({ comparison }: { comparison: EvaluationComparison }) {
  if (!comparison.comparable) return <section aria-label="Evaluation comparison"><h3>Results are not comparable</h3><p>No score delta is available: {(comparison.reasons ?? []).map((reason) => reason.replaceAll("_", " ")).join(", ")}.</p></section>;
  return <section aria-label="Evaluation comparison"><h3>Comparable results</h3>
    {comparison.pairwise?.length ? <p>Pairwise preferences; independent quality scores and quality deltas are not measured.</p> : <table aria-label="Comparable evaluation scores"><thead><tr><th>Left score</th><th>Right score</th><th>Right minus left</th></tr></thead><tbody><tr><td>{comparison.left_score?.toFixed(3) ?? "Unavailable"}</td><td>{comparison.right_score?.toFixed(3) ?? "Unavailable"}</td><td>{comparison.delta == null ? "Unavailable" : `${comparison.delta >= 0 ? "+" : ""}${comparison.delta.toFixed(3)}`}</td></tr></tbody></table>}
    {!!comparison.pairwise?.length && <Preferences cases={comparison.pairwise}/>}
  </section>;
}
