import { useCallback, useEffect, useState } from "react";
import {
  curateAdapter,
  getAdapterLineage,
  listAdapters,
  retireAdapter,
  type Adapter,
} from "../../api/training";
import type { components } from "../../api/schema";
import { EvaluationGroups } from "../evaluations/EvaluationGroups";
import { ConfirmAction } from "../ConfirmAction";
function Ancestry({ id }: { id: string }) {
  const [lineage, setLineage] = useState<components["schemas"]["AdapterLineage"] | null>(null),
    [error, setError] = useState("");
  return (
    <details
      onToggle={(event) => {
        if (event.currentTarget.open && !lineage)
          void getAdapterLineage(id)
            .then(setLineage)
            .catch((e) => setError(String(e)));
      }}
    >
      <summary>Immutable training ancestry</summary>
      {error && <p role="alert">{error}</p>}
      {lineage && (
        <>
          <p>
            {lineage.parent
              ? `Initial parent ${lineage.parent.adapter_id}`
              : "Started from the bare base"}
          </p>
          <ol>
            {lineage.ancestors.map((ancestor) => (
              <li key={ancestor.adapter_id}>
                {ancestor.objective.toUpperCase()} · {ancestor.adapter_id} · job{" "}
                {ancestor.source_job_id} · {ancestor.dataset_ids.length} datasets
                <pre className="mono">{ancestor.target.adapter_manifest_sha256}</pre>
              </li>
            ))}
          </ol>
        </>
      )}
    </details>
  );
}
export function AdapterPanel() {
  const [rows, setRows] = useState<Adapter[] | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [cursor, setCursor] = useState<string | null>(null);
  const reload = useCallback(async () => {
    const page = await listAdapters();
    setRows(page.items);
    setCursor(page.next_cursor ?? null);
  }, []);
  useEffect(() => {
    void reload().catch((e) => setError(String(e)));
  }, [reload]);
  const action = async (work: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await work();
      await reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-label="Adapters">
      <h2>Adapters</h2>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {rows === null && !error && <p role="status">Loading adapters…</p>}
      {rows?.length === 0 && <p>No adapters have been registered.</p>}
      {rows?.map((adapter) => (
        <article className="training-card" key={adapter.id}>
          <h3>{adapter.slug}</h3>
          <p>{adapter.objective.toUpperCase()}</p>
          <Ancestry id={adapter.id} />
          <p className="mono">{adapter.selector}</p>
          <p>
            {adapter.state} ·{" "}
            {adapter.visibility === "published" ? "published" : "private / admin only"} ·{" "}
            {adapter.verified ? "independently verified" : "unverified — write tasks refused"}
          </p>
          <details>
            <summary>Exact target and lineage</summary>
            <pre className="mono">{JSON.stringify(adapter, null, 2)}</pre>
          </details>
          {adapter.state === "ready" && (
            <div className="row">
              <button
                className="button ghost"
                disabled={busy}
                onClick={() =>
                  void action(() =>
                    curateAdapter(
                      adapter,
                      adapter.visibility === "published" ? "admin_only" : "published",
                      crypto.randomUUID(),
                    ),
                  )
                }
              >
                {adapter.visibility === "published" ? "Unpublish" : "Publish"}
              </button>
              <a
                href={`#chat/target/${encodeURIComponent(adapter.selector)}`}
                className="button ghost"
              >
                Select exact adapter in Chat
              </a>
            </div>
          )}
          {adapter.state !== "retired" && (
            <ConfirmAction
              label="Retire"
              target={adapter.slug}
              onConfirm={() => action(() => retireAdapter(adapter, crypto.randomUUID()))}
            />
          )}
          <p>
            Publication requires base readiness, publication and entitlements. Verification never
            inherits from the base.
          </p>
          <EvaluationGroups links={adapter.evaluation_groups ?? []} />
        </article>
      ))}
      {cursor && (
        <button
          className="button ghost"
          onClick={() =>
            void listAdapters(cursor)
              .then((page) => {
                setRows((old) => [...(old ?? []), ...page.items]);
                setCursor(page.next_cursor ?? null);
              })
              .catch((e) => setError(String(e)))
          }
        >
          Load older adapters
        </button>
      )}
    </section>
  );
}
