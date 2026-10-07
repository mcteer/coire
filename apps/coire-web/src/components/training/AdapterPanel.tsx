import { useCallback, useEffect, useState } from "react";
import { curateAdapter, listAdapters, retireAdapter, type Adapter } from "../../api/training";
import { ConfirmAction } from "../ConfirmAction";
export function AdapterPanel() {
  const [rows, setRows] = useState<Adapter[] | null>(null), [error, setError] = useState(""), [busy, setBusy] = useState(false), [cursor, setCursor] = useState<string | null>(null);
  const reload = useCallback(async () => { const page = await listAdapters(); setRows(page.items); setCursor(page.next_cursor ?? null); }, []);
  useEffect(() => { void reload().catch((e) => setError(String(e))); }, [reload]);
  const action = async (work: () => Promise<unknown>) => { if (busy) return; setBusy(true); setError(""); try { await work(); await reload(); } catch (e) { setError(String(e)); } finally { setBusy(false); } };
  return <section aria-label="Adapters"><h2>Adapters</h2>
    {error && <p role="alert" className="error">{error}</p>}{rows === null && !error && <p role="status">Loading adapters…</p>}{rows?.length === 0 && <p>No adapters have been registered.</p>}
    {rows?.map((adapter) => <article className="training-card" key={adapter.id}>
      <h3>{adapter.slug}</h3><p className="mono">{adapter.selector}</p><p>{adapter.state} · {adapter.visibility === "published" ? "published" : "private / admin only"} · {adapter.verified ? "independently verified" : "unverified — write tasks refused"}</p>
      <details><summary>Exact target and lineage</summary><pre className="mono">{JSON.stringify(adapter, null, 2)}</pre></details>
      {adapter.state === "ready" && <div className="row"><button className="button ghost" disabled={busy} onClick={() => void action(() => curateAdapter(adapter, adapter.visibility === "published" ? "admin_only" : "published", crypto.randomUUID()))}>{adapter.visibility === "published" ? "Unpublish" : "Publish"}</button><a href={`#chat/target/${encodeURIComponent(adapter.selector)}`} className="button ghost">Select exact adapter in Chat</a></div>}
      {adapter.state !== "retired" && <ConfirmAction label="Retire" target={adapter.slug} onConfirm={() => action(() => retireAdapter(adapter, crypto.randomUUID()))}/>}
      <p>Publication requires base readiness, publication and entitlements. Verification never inherits from the base.</p>
      <p>Before / after task and judge comparisons unavailable (feature 017).</p>
    </article>)}
    {cursor && <button className="button ghost" onClick={() => void listAdapters(cursor).then((page) => { setRows((old) => [...(old ?? []), ...page.items]); setCursor(page.next_cursor ?? null); }).catch((e) => setError(String(e)))}>Load older adapters</button>}
  </section>;
}
