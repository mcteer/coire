import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  cancelUpgrade,
  getUpgradeOperation,
  getUpgradeTargets,
  listUpgradeReleases,
  rollbackUpgrade,
  startCoreUpgrade,
  startStudioUpgrade,
  upgradeEventsUrl,
  type UpgradeEventPage,
  type UpgradeOperation,
  type UpgradeReleaseSummary,
  type UpgradeRollbackRequest,
  type UpgradeStartRequest,
  type UpgradeTargetList,
} from "../api/client";
import { useEventStream } from "../hooks/useEventStream";

type Target = UpgradeTargetList["items"][number];
const terminal = new Set(["succeeded", "rolled_back", "failed_unchanged", "cancelled_unchanged"]);
const labels: Record<string, string> = {
  succeeded: "Succeeded",
  rolled_back: "Rolled back",
  failed_unchanged: "Failed; source unchanged",
  cancelled_unchanged: "Cancelled; source unchanged",
  recovery_required: "Recovery required",
};
const label = (phase: string) => labels[phase] ?? phase.replaceAll("_", " ");
const targetKey = (target: Target) => target.node ?? "core";
const failure = (error: unknown) =>
  error instanceof Error ? error.message : "Upgrade API unavailable";
const pendingKey = "coire.upgrades.pending";
type PendingChange =
  | { action: "start"; node: "coire-edge-a" | "coire-edge-b" | null; request: UpgradeStartRequest }
  | { action: "rollback"; parentJobId: string; request: UpgradeRollbackRequest };
function retainedRequest(): PendingChange | null {
  try {
    const text = sessionStorage.getItem(pendingKey);
    if (!text || text.length > 8192) return null;
    const value = JSON.parse(text) as PendingChange;
    if (
      !value.request ||
      !/^[a-f0-9]{64}$/.test(value.request.expected_source_sha256) ||
      !/^[a-f0-9-]{36}$/.test(value.request.idempotency_key) ||
      value.request.acknowledge_maintenance !== true
    )
      return null;
    if (
      value.action === "start" &&
      [null, "coire-edge-a", "coire-edge-b"].includes(value.node) &&
      /^[a-f0-9-]{36}$/.test(value.request.release_id)
    )
      return value;
    if (value.action === "rollback" && /^[0-9A-HJKMNP-TV-Z]{26}$/.test(value.parentJobId))
      return value;
    return null;
  } catch {
    return null;
  }
}
export function clearUpgradeRequest() {
  try {
    sessionStorage.removeItem(pendingKey);
  } catch {
    /* Browser storage may be unavailable. */
  }
}

export function UpgradesPage({
  enabled,
  initialJobId = "",
}: {
  enabled: boolean;
  initialJobId?: string;
}) {
  const [targets, setTargets] = useState<Target[]>([]);
  const [releases, setReleases] = useState<UpgradeReleaseSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [targetId, setTargetId] = useState("coire-edge-a");
  const [releaseId, setReleaseId] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [denied, setDenied] = useState(false);
  const [jobId, setJobId] = useState(initialJobId);
  const [observed, setObserved] = useState<UpgradeOperation | null>(null);
  const [pending, setPending] = useState<PendingChange | null>(retainedRequest);
  const lock = useRef(false);
  const requests = useRef(new Map<string, UpgradeStartRequest>());
  const rollbacks = useRef(new Map<string, UpgradeRollbackRequest>());
  const statusFocus = useRef<HTMLElement>(null);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    try {
      const [current, catalog] = await Promise.all([getUpgradeTargets(), listUpgradeReleases()]);
      setTargets(current.items);
      setTargetId((previous) =>
        current.items.some((item) => targetKey(item) === previous)
          ? previous
          : current.items[0]
            ? targetKey(current.items[0])
            : previous,
      );
      setReleases(catalog.items);
      setCursor(catalog.next_cursor ?? null);
      setError("");
      setDenied(false);
    } catch (reason) {
      if (reason instanceof ApiError && [401, 403].includes(reason.status)) setDenied(true);
      setError(failure(reason));
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    setJobId(initialJobId);
  }, [initialJobId]);
  useEffect(() => {
    if (!enabled || !jobId) return;
    let active = true;
    void getUpgradeOperation(jobId)
      .then((value) => {
        if (active) setObserved(value);
      })
      .catch((reason) => {
        if (active) {
          setError(failure(reason));
          if (reason instanceof ApiError && [401, 403].includes(reason.status)) setDenied(true);
        }
      });
    return () => {
      active = false;
    };
  }, [enabled, jobId]);
  const stream = useEventStream<UpgradeEventPage>(
    enabled && jobId ? upgradeEventsUrl(jobId) : "",
    null,
    {
      decode: (frame) => {
        if (frame.event !== "snapshot" && frame.event !== "reset") return undefined;
        const page = JSON.parse(frame.data) as UpgradeEventPage;
        if (page.operation?.job_id !== jobId || !Array.isArray(page.events))
          throw new Error("Invalid upgrade progress snapshot");
        return page;
      },
      isTerminal: (page) => terminal.has(page.operation.phase),
    },
  );
  const operation =
    stream.data?.operation.job_id === jobId
      ? stream.data.operation
      : observed?.job_id === jobId
        ? observed
        : null;
  const accessDenied = denied || stream.accessDenied === true;
  useEffect(() => {
    if (operation?.job_id) statusFocus.current?.focus();
  }, [operation?.job_id]);
  useEffect(() => {
    if (operation && terminal.has(operation.phase)) {
      setAcknowledged(false);
      void refresh();
    }
  }, [operation?.phase, refresh]);

  const target = targets.find((item) => targetKey(item) === targetId);
  const candidates = releases.filter(
    (item) =>
      item.status === "prepared" &&
      item.kind === target?.kind &&
      item.manifest_sha256 !== target.observed_sha256 &&
      item.compatibility.source_sha256s.includes(target.observed_sha256 ?? ""),
  );
  const candidate = candidates.find((item) => item.release_id === releaseId);
  const sourceRelease = releases.find((item) => item.manifest_sha256 === target?.observed_sha256);
  const pairedRequired = candidate?.paired_required || sourceRelease?.paired_required;
  const inProgress = operation && !terminal.has(operation.phase);
  const canStart =
    !accessDenied &&
    !loading &&
    !busy &&
    !pending &&
    !inProgress &&
    acknowledged &&
    target?.readiness === "ready" &&
    !!candidate;
  const selectedTarget = targets.find(
    (item) => item.kind === operation?.kind && item.node === operation?.node,
  );
  const canRollback =
    !accessDenied &&
    !pending &&
    operation?.phase === "succeeded" &&
    selectedTarget?.rollback_eligible &&
    selectedTarget.readiness === "ready" &&
    selectedTarget.observed_sha256 === operation.target_sha256;

  async function mutate(action: () => Promise<UpgradeOperation>) {
    if (lock.current || accessDenied) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const accepted = await action();
      setObserved(accepted);
      setJobId(accepted.job_id);
      window.location.hash = `admin/upgrades/${accepted.job_id}`;
      clearUpgradeRequest();
      setPending(null);
    } catch (reason) {
      if (reason instanceof ApiError && [401, 403].includes(reason.status)) setDenied(true);
      setError(failure(reason));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  function retain(value: PendingChange): boolean {
    try {
      sessionStorage.setItem(pendingKey, JSON.stringify(value));
      setPending(value);
      return true;
    } catch {
      setError(
        "The request could not be saved for recovery. Restore browser storage before submitting.",
      );
      return false;
    }
  }
  function retryPending() {
    if (!pending || busy) return;
    const value = pending;
    void mutate(() =>
      value.action === "rollback"
        ? rollbackUpgrade(value.parentJobId, value.request)
        : value.node
          ? startStudioUpgrade(value.node, value.request)
          : startCoreUpgrade(value.request),
    );
  }
  function start(event: FormEvent) {
    event.preventDefault();
    if (!canStart || !target?.observed_sha256 || !candidate) return;
    const identity = `${targetId}:${target.observed_sha256}:${candidate.release_id}`;
    let request = requests.current.get(identity);
    if (!request) {
      request = {
        release_id: candidate.release_id,
        expected_source_sha256: target.observed_sha256,
        idempotency_key: crypto.randomUUID(),
        acknowledge_maintenance: true,
      };
      requests.current.set(identity, request);
    }
    const body = request;
    if (!retain({ action: "start", node: target.node ?? null, request: body })) return;
    void mutate(() =>
      target.node ? startStudioUpgrade(target.node, body) : startCoreUpgrade(body),
    );
  }
  function rollback() {
    if (!operation || !canRollback || !acknowledged || busy) return;
    let request = rollbacks.current.get(operation.job_id);
    if (!request) {
      request = {
        expected_source_sha256: operation.target_sha256,
        idempotency_key: crypto.randomUUID(),
        acknowledge_maintenance: true,
      };
      rollbacks.current.set(operation.job_id, request);
    }
    const body = request;
    if (!retain({ action: "rollback", parentJobId: operation.job_id, request: body })) return;
    void mutate(() => rollbackUpgrade(operation.job_id, body));
  }
  async function moreReleases() {
    if (!cursor || busy) return;
    setBusy(true);
    try {
      const page = await listUpgradeReleases(cursor);
      setReleases((previous) => [
        ...previous,
        ...page.items.filter((item) => !previous.some((old) => old.release_id === item.release_id)),
      ]);
      setCursor(page.next_cursor ?? null);
    } catch (reason) {
      setError(failure(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!enabled)
    return (
      <main>
        <h1>Upgrades unavailable</h1>
        <p>Managed upgrades require enabled administrator access.</p>
      </main>
    );
  return (
    <main className="grid">
      <section className="panel glass">
        <h1>Upgrades</h1>
        <p>
          Active work drains before affected services change. New affected requests receive a retry
          response during maintenance. Published datasets, adapters and compatible histories remain
          retained.
        </p>
        {error && <p role="alert">{error} Retry uses the same request identity.</p>}
        {pending && !busy && (
          <p>
            An earlier request is awaiting acknowledgment.{" "}
            <button type="button" disabled={accessDenied} onClick={retryPending}>
              Retry previous request
            </button>
          </p>
        )}
        <button type="button" disabled={loading || busy} onClick={() => void refresh()}>
          Refresh readiness
        </button>
        {loading && <p role="status">Loading releases and readiness…</p>}
        {!loading && targets.length === 0 && <p>No managed targets are available.</p>}
        {targets.map((item) => (
          <article key={targetKey(item)} aria-label={`${targetKey(item)} readiness`}>
            <h2>{item.node ?? "Core control plane"}</h2>
            <p>
              Installed version:{" "}
              {releases.find((release) => release.manifest_sha256 === item.observed_sha256)
                ?.version ?? "Unknown"}
            </p>
            <p>
              {label(item.readiness)} · Recovery bootstrap{" "}
              {item.bootstrap_ready ? "verified" : "not ready"}
            </p>
            <dl>
              <dt>Observed release</dt>
              <dd className="mono">{item.observed_sha256 ?? "Unknown"}</dd>
              <dt>Desired release</dt>
              <dd className="mono">{item.desired_sha256 ?? "Unknown"}</dd>
              <dt>Retained source</dt>
              <dd className="mono">{item.retained_sha256 ?? "None"}</dd>
            </dl>
            {item.blocking_reasons?.length ? (
              <ul>
                {item.blocking_reasons.map((reason) => (
                  <li key={reason}>{reason.replaceAll("_", " ")}</li>
                ))}
              </ul>
            ) : null}
            {item.qualifications?.map((qualification) => (
              <p key={qualification.capability}>
                {qualification.capability}: {qualification.state}
                {qualification.reason ? ` · ${qualification.reason.replaceAll("_", " ")}` : ""}
              </p>
            ))}
            {item.rollback_eligible && item.rollback_job_id && (
              <a href={`/#admin/upgrades/${item.rollback_job_id}`}>View retained-source rollback</a>
            )}
            {item.rollback_blocking_reasons?.map((reason) => (
              <p key={reason}>Rollback: {reason.replaceAll("_", " ")}</p>
            ))}
          </article>
        ))}
      </section>
      <section className="panel glass">
        <h2>Prepare a change</h2>
        <form onSubmit={start}>
          <label>
            Target
            <select
              value={targetId}
              disabled={busy || !!inProgress}
              onChange={(event) => {
                setTargetId(event.target.value);
                setReleaseId("");
                setAcknowledged(false);
              }}
            >
              {targets.map((item) => (
                <option key={targetKey(item)} value={targetKey(item)}>
                  {item.node ?? "Core control plane"}
                </option>
              ))}
            </select>
          </label>
          <label>
            Candidate release
            <select
              value={releaseId}
              disabled={busy || !!inProgress}
              onChange={(event) => {
                setReleaseId(event.target.value);
                setAcknowledged(false);
              }}
            >
              <option value="">Choose a compatible prepared release</option>
              {candidates.map((item) => (
                <option key={item.release_id} value={item.release_id}>
                  {item.version} · {item.manifest_sha256}
                </option>
              ))}
            </select>
          </label>
          {candidate && (
            <p>
              Candidate {candidate.version}:{" "}
              <span className="mono">{candidate.manifest_sha256}</span>
              {pairedRequired ? " · Both Studios must drain for paired qualification." : ""}
            </p>
          )}
          {!loading && !candidates.length && (
            <p>No compatible prepared candidate is registered for this source.</p>
          )}
          <label>
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={busy}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            I acknowledge the maintenance window and the displayed affected target.
          </label>
          <button type="submit" disabled={!canStart}>
            Start upgrade
          </button>
          {target?.readiness !== "ready" && (
            <p>The target must be ready before an upgrade can start.</p>
          )}
        </form>
        {cursor && (
          <button disabled={busy} onClick={() => void moreReleases()}>
            Load more prepared releases
          </button>
        )}
      </section>
      {operation && (
        <section
          className="panel glass"
          tabIndex={-1}
          ref={statusFocus}
          aria-label="Upgrade progress"
        >
          <h2>Operation {operation.job_id}</h2>
          <p role="status" aria-live="polite">
            {operation.reason === "expired_operation_recovered" ? "Recovered after deadline; upgrade failed" : label(operation.phase)}
          </p>
          <p>
            Source <span className="mono">{operation.source_sha256}</span>
          </p>
          <p>
            Candidate <span className="mono">{operation.target_sha256}</span>
          </p>
          {operation.reason && <p>{operation.reason.replaceAll("_", " ")}</p>}
          {accessDenied && (
            <p role="alert">
              Upgrade access is no longer available. Restore access and reload to resume.
            </p>
          )}
          {!accessDenied && !terminal.has(operation.phase) && !stream.connected && (
            <p>Progress disconnected. Reconnecting to this operation; refresh resumes by job ID.</p>
          )}
          {operation.phase === "recovery_required" && (
            <p role="alert">
              Recovery required. Maintenance remains in place until source recovery is verified.
            </p>
          )}
          <button
            disabled={
              busy ||
              accessDenied ||
              terminal.has(operation.phase) ||
              operation.phase === "recovery_required" ||
              operation.cancellation_allowed === false ||
              operation.cancel_requested
            }
            onClick={() => void mutate(() => cancelUpgrade(operation.job_id))}
          >
            Request cancellation
          </button>
          {operation.phase === "committing" && operation.cancellation_allowed === false && (
            <p>The selected release is being finalized; cancellation is no longer available.</p>
          )}
          {operation.cancel_requested && (
            <p>Cancellation requested. Waiting for authoritative cleanup or source restoration.</p>
          )}
          <button disabled={busy || !canRollback || !acknowledged} onClick={rollback}>
            Roll back to retained source
          </button>
          {operation.phase === "succeeded" && (
            <p>
              Rollback requires a ready target, retained compatible source and renewed maintenance
              acknowledgment. Compatible schema changes remain in place.
            </p>
          )}
        </section>
      )}
    </main>
  );
}
