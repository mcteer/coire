import type { EvaluationGroupLink } from "../../api/evaluations";
import { EvaluationGroups } from "../evaluations/EvaluationGroups";

export function CheckpointEvaluations({ links, currentCheckpoint }: { links: EvaluationGroupLink[]; currentCheckpoint?: string | null }) {
  const points = links.filter((link) => link.origin === "training_checkpoint");
  if (!points.length) return null;
  return <section aria-label="Checkpoint evaluation points"><h3>Checkpoint score points</h3>
    <p>Current recovery checkpoint: {currentCheckpoint ?? "unavailable"}. Each point keeps its original attempt and fence when training recovers to an earlier update.</p>
    <EvaluationGroups links={points}/>
  </section>;
}
