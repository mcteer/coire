import { useCallback, useEffect, useState } from "react";
import { compareEvaluations, decodeEvaluationGroupEvent, evaluationGroupEventsUrl, getEvaluationGroup, type EvaluationComparison, type EvaluationGroup, type EvaluationGroupEvent, type EvaluationGroupLink } from "../../api/evaluations";
import { useEventStream } from "../../hooks/useEventStream";
import { EvaluationDetail } from "./EvaluationDetail";
import { Comparison } from "./Comparison";
import { CheckpointScorePoints } from "../training/CheckpointScorePoints";
function Group({ link, maximumUpdate }: { link: EvaluationGroupLink; maximumUpdate: number }) {
  const [group, setGroup] = useState<EvaluationGroup | null>(null), [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [comparisons, setComparisons] = useState<Record<string, EvaluationComparison>>({});
  const refresh = useCallback(async () => { if (!link.group_id) return; try { setGroup(await getEvaluationGroup(link.group_id)); setError(""); } catch (cause) { setError(String(cause)); } }, [link.group_id]);
  useEffect(() => { void refresh(); }, [refresh]);
  const stream = useEventStream<EvaluationGroupEvent>(link.group_id && (!group || ["pending", "running"].includes(group.state)) ? evaluationGroupEventsUrl(link.group_id) : "", null, { decode: (frame) => decodeEvaluationGroupEvent(frame, link.group_id!), isTerminal: (event) => event.kind === "terminal" });
  useEffect(() => { if (stream.data?.snapshot) setGroup(stream.data.snapshot); else if (stream.data) void refresh(); }, [stream.data, refresh]);
  useEffect(() => { if (!link.group_id || (group && !["pending", "running"].includes(group.state))) return; const timer = window.setInterval(() => void refresh(), 5000); return () => window.clearInterval(timer); }, [link.group_id, group, refresh]);
  return <article className="training-card"><h4>{link.origin.replaceAll("_", " ")} · update {link.completed_update ?? "unavailable"}</h4><p role="status">Evaluation {group?.state ?? link.state}</p>
    {link.origin === "training_checkpoint" && <><p className="mono">Checkpoint {link.checkpoint_id} · attempt {link.attempt_id ?? "unavailable"} · fence {link.fence ?? "unavailable"}</p><p>Pause owner: {link.pause_owner ?? "unavailable"} · boundary phase: {link.trigger_phase ?? "unavailable"}{link.resume_disposition ? ` · resume: ${link.resume_disposition}` : ""}</p></>}
    {group && link.origin === "training_checkpoint" && <CheckpointScorePoints group={group} link={link} maximumUpdate={maximumUpdate}/>}
    {!link.group_id && <p>Durable evaluation obligation pending admission.</p>}{error && <p role="alert">{error}</p>}{stream.error && <p>{stream.error}</p>}
    {group?.runs?.map((run) => <div key={run.id}><button onClick={() => setSelected(selected === run.id ? null : run.id)}>{run.suite.suite_id} version {run.suite.version}: {run.state}</button>
      {run.result && run.result.subjects.length === 2 && <button onClick={() => void compareEvaluations(run.result!.id, 0, run.result!.id, 1).then((comparison) => setComparisons((old) => ({ ...old, [run.id]: comparison }))).catch((cause) => setError(String(cause)))}>Compare base and adapter for {run.suite.suite_id}</button>}
      {comparisons[run.id] && <Comparison comparison={comparisons[run.id]}/>}{selected === run.id && <EvaluationDetail id={run.id}/>}</div>)}
  </article>;
}
export function EvaluationGroups({ links }: { links: EvaluationGroupLink[] }) {
  const maximumUpdate = Math.max(1, ...links.map((link) => link.completed_update ?? 0));
  return <section aria-label="Training evaluations"><h3>Evaluation comparisons</h3><p>Evaluation outcomes are independent of training success and exact-target harness verification.</p>{links.length ? links.map((link) => <Group key={link.group_id ?? link.trigger_id} link={link} maximumUpdate={maximumUpdate}/>) : <p>No evaluation obligations were declared for this run.</p>}</section>;
}
