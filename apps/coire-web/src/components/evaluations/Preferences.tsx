import type { components } from "../../api/schema";
export function Preferences({ cases }: { cases: components["schemas"]["EvaluationPairwiseCase"][] }) {
  const count = (subject: number | null) => cases.filter((item) => item.preferred_subject === subject).length;
  return <section aria-label="Counterbalanced preferences"><h4>Pairwise preferences</h4><p>Left wins {count(0)} · ties {count(null)} · right wins {count(1)}. Preferences are separate from independent quality scores; disagreements between presentation orders count as ties.</p>
    <table aria-label="Preference cases"><thead><tr><th>Case</th><th>First order</th><th>First choice</th><th>Reversed choice</th><th>Preference</th></tr></thead><tbody>{cases.map((item) => <tr key={item.case_id}><td>{item.case_id}</td><td>{item.first_order}</td><td>{item.first.winner}</td><td>{item.second.winner}</td><td>{item.preferred_subject === null ? "Tie" : item.preferred_subject === 0 ? "Left" : "Right"}</td></tr>)}</tbody></table>
  </section>;
}
