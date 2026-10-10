import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  cancelFeedbackExport,
  listFeedbackExports,
  type PreferenceExportDetail,
} from "../../api/feedback";
import { getDataset, type Dataset } from "../../api/training";

export function ExportHistory({
  refreshKey,
  onDataset,
}: {
  refreshKey: number;
  onDataset?: (id: string) => void;
}) {
  const [rows, setRows] = useState<PreferenceExportDetail[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [datasets, setDatasets] = useState<Record<string, Dataset>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const cached = useRef<Record<string, Dataset>>({});
  const cancelKeys = useRef<Record<string, string>>({});
  const expanded = useRef(false);
  const reload = useCallback(async () => {
    const page = await listFeedbackExports();
    setRows((old) => [
      ...page.items,
      ...(expanded.current
        ? (old ?? []).filter((row) => !page.items.some((fresh) => fresh.id === row.id))
        : []),
    ]);
    if (!expanded.current) setCursor(page.next_cursor ?? null);
  }, []);
  useEffect(() => {
    let current = true;
    const refresh = () => {
      if (current)
        void reload().catch(() => {
          if (current) setError("Export history is unavailable.");
        });
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [refreshKey, reload]);
  useEffect(() => {
    let current = true;
    for (const row of rows ?? []) {
      const id = row.dataset_id;
      if (!id || cached.current[id]?.state === "ready") continue;
      void getDataset(id)
        .then((dataset) => {
          if (current) {
            cached.current[id] = dataset;
            setDatasets((old) => ({ ...old, [id]: dataset }));
          }
        })
        .catch(() => {
          if (current)
            setError("Published dataset readiness is unavailable. Publication remains recorded.");
        });
    }
    return () => {
      current = false;
    };
  }, [rows]);
  const cancel = async (row: PreferenceExportDetail) => {
    if (busy) return;
    setBusy(row.id);
    setError("");
    const command = `${row.id}:${row.version}`;
    const key = cancelKeys.current[command] ?? crypto.randomUUID();
    cancelKeys.current[command] = key;
    try {
      await cancelFeedbackExport(row.id, row.version, key);
      await reload();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setError(
          "Export changed. Current progress has been refreshed; published datasets cannot be cancelled.",
        );
        await reload().catch(() => {});
      } else
        setError("Cancellation could not be confirmed. Inspect current progress before retrying.");
    } finally {
      setBusy(null);
    }
  };
  return (
    <section aria-label="Feedback export history">
      <h2>Export history</h2>
      {error && <p role="alert">{error}</p>}
      {rows === null && !error && <p role="status">Loading exports…</p>}
      {rows?.length === 0 && <p>No feedback exports yet.</p>}
      {rows?.map((row) => (
        <article className="training-card" key={row.id} aria-label={row.request.name}>
          <h3>
            {row.request.name} <span className="status">{row.state}</span>
          </h3>
          <p className="mono">{row.id}</p>
          <p>
            {row.matched_count} matches · {row.selected_count} selected · {row.excluded_count}{" "}
            excluded
          </p>
          <p>
            Limit: {row.pair_limit.toLocaleString()} pairs and {row.byte_limit / 1024 ** 2} MiB.
          </p>
          {row.warnings?.includes("small_sample") && (
            <p role="status">
              Small sample: fewer than 20 pairs. Interpret training results cautiously.
            </p>
          )}
          {row.reason && (
            <p>
              {row.reason === "insufficient_groups"
                ? "At least two distinct prompt groups are required."
                : row.reason === "oversize"
                  ? "The selection exceeds the export limits."
                  : `Reason: ${row.reason.replaceAll("_", " ")}`}
            </p>
          )}
          {row.cleanup_pending && (
            <p role="status">Private cleanup is pending; storage remains reserved.</p>
          )}
          {row.dataset_id && (
            <div>
              <p>
                Training readiness:{" "}
                {datasets[row.dataset_id]?.state.replaceAll("_", " ") ??
                  "checking dataset analysis"}
              </p>
              <a href="#training" onClick={() => onDataset?.(row.dataset_id!)}>
                View private dataset
              </a>
            </div>
          )}
          {!row.dataset_id && ["queued", "staging"].includes(row.state) && (
            <button
              type="button"
              className="button"
              disabled={busy !== null}
              onClick={() => void cancel(row)}
            >
              {busy === row.id ? "Cancelling…" : "Cancel export"}
            </button>
          )}
          <details>
            <summary>Selection and deadline</summary>
            <p>Judgement source: {row.request.source ?? "owner_preferred"}.</p>
            <p>Deadline: {new Date(row.deadline).toLocaleString()}.</p>
          </details>
        </article>
      ))}
      {cursor && (
        <button
          type="button"
          className="button"
          onClick={() =>
            void listFeedbackExports(cursor)
              .then((page) => {
                expanded.current = true;
                setRows((old) => [
                  ...(old ?? []),
                  ...page.items.filter((row) => !(old ?? []).some((saved) => saved.id === row.id)),
                ]);
                setCursor(page.next_cursor ?? null);
              })
              .catch(() => setError("Older export history is unavailable."))
          }
        >
          Load older exports
        </button>
      )}
    </section>
  );
}
