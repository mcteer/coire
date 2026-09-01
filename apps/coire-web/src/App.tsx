import { type FormEvent, useEffect, useState } from "react";
import {
  api,
  type ActivityItem,
  type ActivityPage,
  type ApiKey,
  type ApiKeyIssued,
  type AuditRecord,
  type ConsoleSnapshot,
  type ModelVariant,
  type User,
} from "./api/client";
import { AskCoire } from "./pages/admin/AskCoire";
import { useEventStream } from "./hooks/useEventStream";
import { ConfirmAction } from "./components/ConfirmAction";
import "./styles/app.css";
type Tab = "overview" | "models" | "instances" | "activity" | "identity" | "audit";
const TABS: [Tab, string][] = [
  ["overview", "Overview"],
  ["models", "Models"],
  ["instances", "Instances"],
  ["activity", "Runs & jobs"],
  ["identity", "Users & keys"],
  ["audit", "Audit"],
];
const gb = (n: number) => `${(n / 1024 ** 3).toFixed(1)} GB`;
function Shell({
  tab,
  setTab,
  children,
  snapshot,
}: {
  tab: Tab;
  setTab: (t: Tab) => void;
  children: React.ReactNode;
  snapshot: ConsoleSnapshot | null;
}) {
  const health = snapshot?.cluster.nodes.some((n) => n.reachability !== "healthy")
    ? "degraded"
    : "healthy";
  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <span className="logo">C</span>
          <b>Coire</b>
          <span className="muted">
            / Admin / <b>{TABS.find(([id]) => id === tab)?.[1]}</b>
          </span>
        </div>
        <div className="chips">
          <span className="chip">
            <i className={`dot ${health}`} />
            {health}
          </span>
          <span className="chip mono">
            {snapshot ? new Date(snapshot.observed_at).toLocaleTimeString() : "connecting"}
          </span>
          <span className="logo">M</span>
        </div>
      </header>
      <nav className="tabs glass" aria-label="Admin sections">
        {TABS.map(([id, label]) => (
          <button
            className={`tab ${tab === id ? "active" : ""}`}
            aria-current={tab === id ? "page" : undefined}
            onClick={() => setTab(id)}
            key={id}
          >
            {label}
          </button>
        ))}
      </nav>
      {children}
      <nav className="dock glass" aria-label="Primary">
        <a href="#chat">Chat</a>
        <a href="#training">Training</a>
        <a href="#images">Images</a>
        <a href="#settings">Settings</a>
        <a className="active" aria-current="page" href="#admin">
          Admin
        </a>
      </nav>
    </div>
  );
}
export function Overview({ snapshot }: { snapshot: ConsoleSnapshot }) {
  return (
    <main className="grid">
      {snapshot.ledgers.map((l) => {
        const n = snapshot.cluster.nodes.find((n) => n.id === l.node_id),
          used = l.budget_bytes - l.free_bytes;
        return (
          <section className="panel glass node-card" key={l.node_id}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h3>{l.node_name}</h3>
              <span className="status">
                <i className={`dot ${l.health}`} />
                {l.health}
                {n?.stale ? " · stale" : ""}
              </span>
            </div>
            <div className="capacity" title={`${used} bytes reserved`}>
              <span style={{ width: `${Math.max(0, (used / l.budget_bytes) * 100)}%` }} />
            </div>
            <p className="mono">
              {gb(used)} / {gb(l.budget_bytes)}
            </p>
            <div className="facts">
              <span className="fact">
                Free memory<b>{gb(l.free_bytes)}</b>
              </span>
              <span className="fact">
                Disk free<b>{n?.disk_free_bytes == null ? "—" : gb(n.disk_free_bytes)}</b>
              </span>
              <span className="fact">
                CPU<b>{n?.cpu_percent?.toFixed(0) ?? "—"}%</b>
              </span>
              <span className="fact">
                GPU<b>{n?.gpu_percent?.toFixed(0) ?? "—"}%</b>
              </span>
              <span className="fact">
                Thermal<b>{n?.thermal_state ?? "unknown"}</b>
              </span>
              <span className="fact">
                Heartbeat
                <b>
                  {l.health_sampled_at
                    ? new Date(l.health_sampled_at).toLocaleTimeString()
                    : "stale"}
                </b>
              </span>
            </div>
            {l.health_reason && <p className="error">{l.health_reason}</p>}
          </section>
        );
      })}
      <section className="panel glass wide">
        <h3>Instances & alerts</h3>
        {snapshot.cluster.instances.length === 0 ? (
          <p className="empty">No model instances are running.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Instance</th>
                <th>Policy</th>
                <th>State</th>
                <th>Members</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.cluster.instances.map((i) => (
                <tr key={i.id}>
                  <td className="mono">{i.id.slice(0, 8)}</td>
                  <td>{i.policy}</td>
                  <td>{i.effective_state}</td>
                  <td>{i.members?.length ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {snapshot.alerts?.map((a) => (
          <p className="error" key={a.title}>
            <b>{a.title}</b> — {a.detail}
          </p>
        ))}
      </section>
      <AskCoire />
    </main>
  );
}
export function DataPage({ kind }: { kind: "models" | "instances" }) {
  const [rows, setRows] = useState<Record<string, unknown>[] | null>(null),
    [error, setError] = useState("");
  const [repo, setRepo] = useState("");
  const [precision, setPrecision] = useState("4bit");
  const [editing, setEditing] = useState<Record<string, unknown> | null>(null);
  const [variants, setVariants] = useState<ModelVariant[]>([]);
  const [hasMore, setHasMore] = useState(false);
  useEffect(() => {
    void api<Record<string, unknown>[]>(
      `/api/v1/${kind === "models" ? "admin/models?limit=50" : "instances"}`,
    )
      .then((result) => {
        setRows(result);
        setHasMore(kind === "models" && result.length === 50);
      })
      .catch((e) => setError(String(e)));
  }, [kind]);
  const reload = () => {
    void api<Record<string, unknown>[]>(
      `/api/v1/${kind === "models" ? "admin/models?limit=50" : "instances"}`,
    ).then((result) => {
      setRows(result);
      setHasMore(kind === "models" && result.length === 50);
    });
  };
  const loadOlder = async () => {
    if (kind !== "models" || !rows?.length) return;
    const last = rows.at(-1);
    const before = encodeURIComponent(String(last?.created_at));
    const older = await api<Record<string, unknown>[]>(
      `/api/v1/admin/models?limit=50&before=${before}&before_id=${String(last?.id)}`,
    );
    setRows([...rows, ...older]);
    setHasMore(older.length === 50);
  };
  const mutate = async (path: string, method = "POST", body?: object) => {
    setError("");
    try {
      await api(path, { method, body: body ? JSON.stringify(body) : undefined });
      reload();
    } catch (error) {
      setError(String(error));
    }
  };
  const acquire = async (event: FormEvent) => {
    event.preventDefault();
    const bits = precision === "4bit" ? 4 : precision === "6bit" ? 6 : undefined;
    await mutate("/api/v1/admin/models/acquisitions", "POST", {
      repo_id: repo,
      variant: {
        name: precision,
        precision,
        bits,
        group_size: bits ? 64 : undefined,
        mode: bits ? "affine" : undefined,
      },
    });
    setRepo("");
  };
  const saveCuration = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editing) return;
    const form = new FormData(event.currentTarget);
    await api(`/api/v1/admin/models/${String(editing.id)}`, {
      method: "PATCH",
      headers: { "If-Match": String(editing.updated_at) },
      body: JSON.stringify({
        display_name: form.get("display_name"),
        description: form.get("description"),
        tags: form.getAll("tags").map(String),
        entitlement: String(form.get("entitlement") ?? "")
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
        placement_policy: form.get("placement_policy"),
        idle_ttl_seconds: Number(form.get("idle_ttl_seconds")),
        visibility: form.get("visibility"),
        capability_profile: {
          tool_calling: form.get("tool_calling"),
          structured_output: form.get("structured_output"),
          reasoning: form.get("reasoning"),
          context_window: Number(form.get("context_window")),
          parallel_tools: form.get("parallel_tools") === "on",
        },
      }),
    });
    setEditing(null);
    reload();
  };
  const showVariants = async (modelId: string) =>
    setVariants(await api<ModelVariant[]>(`/api/v1/admin/models/${modelId}/variants`));
  return (
    <main className="panel glass">
      <h2>{kind === "models" ? "Model roster" : "Instances"}</h2>
      {kind === "models" && (
        <form className="row" onSubmit={acquire}>
          <div className="field">
            <label htmlFor="repo">Hugging Face repository</label>
            <input
              id="repo"
              placeholder="organisation/model"
              pattern="[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
              value={repo}
              onChange={(event) => setRepo(event.target.value)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="precision">Target precision</label>
            <select
              id="precision"
              value={precision}
              onChange={(event) => setPrecision(event.target.value)}
            >
              <option value="4bit">4-bit · g64</option>
              <option value="6bit">6-bit · g64</option>
              <option value="bf16">BF16</option>
            </select>
          </div>
          <button className="button">Add from Hugging Face</button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
      {!rows ? (
        <p>Loading…</p>
      ) : rows.length === 0 ? (
        <p className="empty">No {kind}.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>State</th>
              <th>Placement</th>
              {kind === "models" && <th>Acquisition / copies</th>}
              <th>Updated</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={String(r.id ?? i)}>
                <td>{String(r.display_name ?? r.slug ?? r.id)}</td>
                <td>{String(r.state ?? "—")}</td>
                <td>{String(r.placement_policy ?? r.policy ?? "—")}</td>
                {kind === "models" && (
                  <td>
                    {r.job && typeof r.job === "object" ? (
                      <div>
                        <b>{String((r.job as Record<string, unknown>).stage)}</b>{" "}
                        {Number((r.job as Record<string, unknown>).percent ?? 0).toFixed(0)}%
                        {(r.job as Record<string, unknown>).failure_reason != null && (
                          <p className="error">
                            {String((r.job as Record<string, unknown>).failure_reason)}
                          </p>
                        )}
                      </div>
                    ) : (
                      <span className="muted">No active acquisition</span>
                    )}
                    {(Array.isArray(r.copies) ? r.copies : []).map((copy) => {
                      const value = copy as Record<string, unknown>;
                      return (
                        <div className="mono" key={String(value.node)}>
                          {String(value.node)} · {gb(Number(value.bytes ?? 0))} ·{" "}
                          {value.verified ? "verified" : "copying"}
                        </div>
                      );
                    })}
                  </td>
                )}
                <td>{String(r.updated_at ?? "—")}</td>
                <td>
                  <div className="actions">
                    {kind === "models" ? (
                      <>
                        {r.state === "ready" && (
                          <button
                            className="button"
                            onClick={() => void mutate(`/api/v1/admin/models/${String(r.id)}/load`)}
                          >
                            Load
                          </button>
                        )}
                        {r.state === "failed" && (
                          <button
                            className="button"
                            onClick={() =>
                              void mutate(`/api/v1/admin/models/${String(r.id)}/retry`)
                            }
                          >
                            Retry
                          </button>
                        )}
                        <button className="button" onClick={() => setEditing(r)}>
                          Curate
                        </button>
                        <button className="button" onClick={() => void showVariants(String(r.id))}>
                          Variants
                        </button>
                        <button
                          className="button"
                          onClick={() =>
                            void api(`/api/v1/admin/models/${String(r.id)}`, {
                              method: "PATCH",
                              headers: { "If-Match": String(r.updated_at) },
                              body: JSON.stringify({
                                visibility:
                                  r.visibility === "published" ? "admin_only" : "published",
                              }),
                            })
                              .then(reload)
                              .catch((error) => setError(String(error)))
                          }
                        >
                          {r.visibility === "published" ? "Unpublish" : "Publish"}
                        </button>
                        <button
                          className="button"
                          onClick={() =>
                            void api(`/api/v1/admin/models/${String(r.id)}`, {
                              method: "PATCH",
                              headers: { "If-Match": String(r.updated_at) },
                              body: JSON.stringify({
                                placement_policy: String(r.placement_policy).startsWith("pinned:")
                                  ? "single:auto"
                                  : "pinned:coire-edge-b",
                              }),
                            })
                              .then(reload)
                              .catch((error) => setError(String(error)))
                          }
                        >
                          {String(r.placement_policy).startsWith("pinned:") ? "Unpin" : "Pin"}
                        </button>
                        <button
                          className="button"
                          onClick={() =>
                            void mutate(`/api/v1/admin/models/${String(r.id)}/variants`, "POST", {
                              name: "6bit",
                              precision: "6bit",
                              bits: 6,
                              group_size: 64,
                              mode: "affine",
                            })
                          }
                        >
                          Convert
                        </button>
                        {(Array.isArray(r.engines) ? r.engines : []).map((engine) => {
                          const value = engine as Record<string, unknown>;
                          return !["stopped", "failed"].includes(String(value.state)) ? (
                            <ConfirmAction
                              key={String(value.id)}
                              target={`${String(r.display_name ?? r.slug)} on ${String(value.node)}`}
                              label="Unload"
                              onConfirm={() =>
                                mutate(`/api/v1/admin/engines/${String(value.id)}`, "DELETE")
                              }
                            />
                          ) : null;
                        })}
                        <ConfirmAction
                          target={String(r.display_name ?? r.slug)}
                          label="Retire"
                          onConfirm={() => mutate(`/api/v1/admin/models/${String(r.id)}/retire`)}
                        />
                      </>
                    ) : (
                      <ConfirmAction
                        target={String(r.id).slice(0, 8)}
                        label="Stop"
                        onConfirm={() => mutate(`/api/v1/instances/${String(r.id)}`, "DELETE")}
                      />
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editing && (
        <form className="panel" onSubmit={saveCuration}>
          <h3>Curate {String(editing.display_name)}</h3>
          <div className="field">
            <label htmlFor="curation-name">Display name</label>
            <input
              id="curation-name"
              name="display_name"
              defaultValue={String(editing.display_name)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="curation-description">Description</label>
            <textarea
              id="curation-description"
              name="description"
              defaultValue={String(editing.description ?? "")}
            />
          </div>
          <div className="field">
            <label htmlFor="curation-tags">Tags</label>
            <select
              id="curation-tags"
              name="tags"
              multiple
              defaultValue={Array.isArray(editing.tags) ? editing.tags.map(String) : []}
            >
              {["coding", "general", "reasoning", "vision", "image"].map((tag) => (
                <option value={tag} key={tag}>
                  {tag}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="curation-entitlement">Entitlements</label>
            <input
              id="curation-entitlement"
              name="entitlement"
              defaultValue={
                Array.isArray(editing.entitlement) ? editing.entitlement.join(", ") : ""
              }
            />
          </div>
          <div className="field">
            <label htmlFor="curation-placement">Placement</label>
            <input
              id="curation-placement"
              name="placement_policy"
              defaultValue={String(editing.placement_policy)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="curation-ttl">Idle TTL seconds</label>
            <input
              id="curation-ttl"
              name="idle_ttl_seconds"
              type="number"
              min="60"
              defaultValue={Number(editing.idle_ttl_seconds ?? 900)}
            />
          </div>
          <div className="field">
            <label htmlFor="curation-visibility">Visibility</label>
            <select
              id="curation-visibility"
              name="visibility"
              defaultValue={String(editing.visibility)}
            >
              <option value="admin_only">Admin only</option>
              <option value="published">Published</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="curation-tools">Tool calling</label>
            <select
              id="curation-tools"
              name="tool_calling"
              defaultValue={String(
                (editing.capability_profile as Record<string, unknown> | undefined)?.tool_calling ??
                  "none",
              )}
            >
              <option value="none">None</option>
              <option value="prompted">Prompted</option>
              <option value="native">Native</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="curation-output">Structured output</label>
            <select
              id="curation-output"
              name="structured_output"
              defaultValue={String(
                (editing.capability_profile as Record<string, unknown> | undefined)
                  ?.structured_output ?? "none",
              )}
            >
              <option value="none">None</option>
              <option value="json_mode">JSON mode</option>
              <option value="json_schema">JSON schema</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="curation-reasoning">Reasoning</label>
            <select
              id="curation-reasoning"
              name="reasoning"
              defaultValue={String(
                (editing.capability_profile as Record<string, unknown> | undefined)?.reasoning ??
                  "none",
              )}
            >
              <option value="none">None</option>
              <option value="thinking">Thinking</option>
              <option value="hybrid">Hybrid</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="curation-context">Context window</label>
            <input
              id="curation-context"
              name="context_window"
              type="number"
              min="1"
              defaultValue={Number(
                (editing.capability_profile as Record<string, unknown> | undefined)
                  ?.context_window ?? 32768,
              )}
            />
          </div>
          <label className="row">
            <input
              name="parallel_tools"
              type="checkbox"
              defaultChecked={Boolean(
                (editing.capability_profile as Record<string, unknown> | undefined)?.parallel_tools,
              )}
            />
            Parallel tools
          </label>
          <div className="actions">
            <button className="button">Save curation</button>
            <button className="button" type="button" onClick={() => setEditing(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {variants.length > 0 && (
        <section className="panel">
          <h3>Variants</h3>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Precision</th>
                <th>Validation</th>
                <th>Publication</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {variants.map((variant) => (
                <tr key={variant.id}>
                  <td>{variant.name}</td>
                  <td>{variant.precision}</td>
                  <td>
                    {variant.validated
                      ? "verified"
                      : (variant.validation?.smoke_failure ?? variant.state)}
                  </td>
                  <td>
                    {variant.is_default ? "default" : variant.published ? "published" : "hidden"}
                  </td>
                  <td>
                    <div className="actions">
                      <button
                        className="button"
                        onClick={() =>
                          void api(
                            `/api/v1/admin/models/${variant.model_id}/variants/${variant.id}`,
                            {
                              method: "PATCH",
                              headers: { "If-Match": variant.updated_at },
                              body: JSON.stringify({ published: !variant.published }),
                            },
                          ).then(() => showVariants(variant.model_id))
                        }
                      >
                        {variant.published ? "Unpublish" : "Publish"}
                      </button>
                      {variant.validated && !variant.is_default && (
                        <button
                          className="button"
                          onClick={() =>
                            void api(
                              `/api/v1/admin/models/${variant.model_id}/variants/${variant.id}`,
                              {
                                method: "PATCH",
                                headers: { "If-Match": variant.updated_at },
                                body: JSON.stringify({ published: true, is_default: true }),
                              },
                            ).then(() => showVariants(variant.model_id))
                          }
                        >
                          Make default
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {hasMore && (
        <button className="button" onClick={() => void loadOlder()}>
          Load older models
        </button>
      )}
    </main>
  );
}
export function IdentityPage() {
  const [users, setUsers] = useState<User[]>([]),
    [error, setError] = useState("");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [secret, setSecret] = useState<ApiKeyIssued | null>(null);
  const [keys, setKeys] = useState<Record<string, ApiKey[]>>({});
  const [hasMoreUsers, setHasMoreUsers] = useState(false);
  const load = () =>
    void api<User[]>("/api/v1/admin/users?limit=50").then((result) => {
      setUsers(result);
      setHasMoreUsers(result.length === 50);
    });
  useEffect(() => {
    void api<User[]>("/api/v1/admin/users?limit=50")
      .then((result) => {
        setUsers(result);
        setHasMoreUsers(result.length === 50);
      })
      .catch((e) => setError(String(e)));
  }, []);
  const createUser = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api<User>("/api/v1/admin/users", {
        method: "POST",
        body: JSON.stringify({ email, display_name: displayName, role: "user" }),
      });
      setEmail("");
      setDisplayName("");
      load();
    } catch (cause) {
      setError(String(cause));
    }
  };
  const issueKey = async (user: User) => {
    setSecret(
      await api<ApiKeyIssued>(`/api/v1/admin/users/${user.id}/keys`, {
        method: "POST",
        body: JSON.stringify({
          name: "Console key",
          scopes: ["models:read", "chat:write"],
          requests_per_minute: 60,
          monthly_budget_tokens: 1000000,
        }),
      }),
    );
    await loadKeys(user);
  };
  const loadKeys = async (user: User) => {
    const result = await api<ApiKey[]>(`/api/v1/admin/users/${user.id}/keys`);
    setKeys((current) => ({ ...current, [user.id]: result }));
  };
  const updateUser = async (user: User, body: object) => {
    await api(`/api/v1/admin/users/${user.id}`, {
      method: "PATCH",
      headers: { "If-Match": user.updated_at },
      body: JSON.stringify(body),
    });
    load();
  };
  const updateEntitlement = async (user: User, grant: boolean) => {
    await api(`/api/v1/admin/users/${user.id}/entitlements/explicit_image`, {
      method: grant ? "PUT" : "DELETE",
    });
    load();
  };
  const revokeKey = async (user: User, key: ApiKey) => {
    await api(`/api/v1/admin/keys/${key.id}`, { method: "DELETE" });
    await loadKeys(user);
  };
  const loadOlderUsers = async () => {
    if (!users.length) return;
    const last = users.at(-1);
    const before = encodeURIComponent(last?.created_at ?? "");
    const older = await api<User[]>(
      `/api/v1/admin/users?limit=50&before=${before}&before_id=${last?.id ?? ""}`,
    );
    setUsers([...users, ...older]);
    setHasMoreUsers(older.length === 50);
  };
  return (
    <main className="panel glass">
      <h2>Users & keys</h2>
      {error && <p className="error">{error}</p>}
      {secret && (
        <aside className="error" role="status">
          <b>Copy this key now. It will not be shown again.</b>
          <p className="mono">{secret.secret}</p>
          <button className="button" onClick={() => setSecret(null)}>
            I have stored it
          </button>
        </aside>
      )}
      <form className="row" onSubmit={createUser}>
        <div className="field">
          <label htmlFor="display-name">Display name</label>
          <input
            id="display-name"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
          />
        </div>
        <button className="button">Create user</button>
      </form>
      <table>
        <thead>
          <tr>
            <th>User</th>
            <th>Role</th>
            <th>Entitlements</th>
            <th>State</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>
                <b>{u.display_name}</b>
                <br />
                <span className="muted">{u.email}</span>
              </td>
              <td>{u.role}</td>
              <td>{u.entitlements?.join(", ") || "—"}</td>
              <td>{u.active ? "active" : "inactive"}</td>
              <td>
                <div className="actions">
                  <button className="button" onClick={() => void issueKey(u)}>
                    Issue key
                  </button>
                  <button className="button" onClick={() => void loadKeys(u)}>
                    View keys
                  </button>
                  <button
                    className="button"
                    onClick={() =>
                      void updateUser(u, { role: u.role === "admin" ? "user" : "admin" })
                    }
                  >
                    {u.role === "admin" ? "Make user" : "Make admin"}
                  </button>
                  <button
                    className="button"
                    onClick={() =>
                      void updateEntitlement(
                        u,
                        !(u.entitlements?.includes("explicit_image") ?? false),
                      )
                    }
                  >
                    {u.entitlements?.includes("explicit_image")
                      ? "Revoke explicit"
                      : "Grant explicit"}
                  </button>
                </div>
                {keys[u.id]?.map((key) => (
                  <div className="row" key={key.id}>
                    <span className="mono">
                      {key.prefix}… · {key.tokens_consumed}/{key.monthly_budget_tokens}
                    </span>
                    {key.active && (
                      <ConfirmAction
                        target={key.name}
                        label="Revoke"
                        onConfirm={() => revokeKey(u, key)}
                      />
                    )}
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {hasMoreUsers && (
        <button className="button" onClick={() => void loadOlderUsers()}>
          Load older users
        </button>
      )}
    </main>
  );
}
export function ActivityPage() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState("");
  const load = async () => {
    try {
      const page = await api<ActivityPage>("/api/v1/admin/console/activity?limit=50");
      setItems(page.items);
      setNextCursor(page.next_cursor ?? null);
    } catch (cause) {
      setError(String(cause));
    }
  };
  const loadOlder = async () => {
    if (!nextCursor) return;
    const [before, beforeId] = nextCursor.split("|");
    const page = await api<ActivityPage>(
      `/api/v1/admin/console/activity?limit=50&before=${encodeURIComponent(before ?? "")}&before_id=${beforeId ?? ""}`,
    );
    setItems([...items, ...page.items]);
    setNextCursor(page.next_cursor ?? null);
  };
  useEffect(() => void load(), []);
  return (
    <main className="panel glass">
      <h2>Runs & jobs</h2>
      {error && <p className="error">{error}</p>}
      {items.length === 0 ? (
        <p className="empty">
          No shipped work is running. Agent-run controls remain absent until that capability ships.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Kind</th>
              <th>Target</th>
              <th>Owner</th>
              <th>State</th>
              <th>Elapsed / progress</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>{item.kind}</td>
                <td>{item.target}</td>
                <td>{item.owner}</td>
                <td>{item.state}</td>
                <td className="mono">
                  {Math.round(item.elapsed_seconds)}s
                  {item.progress_percent == null ? "" : ` · ${item.progress_percent.toFixed(0)}%`}
                </td>
                <td>
                  {item.kind === "instance" && item.can_stop ? (
                    <ConfirmAction
                      target={String(item.id).slice(0, 8)}
                      label="Stop"
                      onConfirm={async () => {
                        await api(`/api/v1/instances/${String(item.id)}`, { method: "DELETE" });
                        await load();
                      }}
                    />
                  ) : item.kind === "job" && item.can_stop ? (
                    <ConfirmAction
                      target={String(item.target)}
                      label="Cancel"
                      onConfirm={async () => {
                        await api(`/api/v1/admin/jobs/${String(item.id)}`, { method: "DELETE" });
                        await load();
                      }}
                    />
                  ) : (
                    <span className="muted">Observe only</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {nextCursor && (
        <button className="button" onClick={() => void loadOlder()}>
          Load older activity
        </button>
      )}
    </main>
  );
}
function AuditPage() {
  const [rows, setRows] = useState<AuditRecord[]>([]);
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [hasMoreAudit, setHasMoreAudit] = useState(false);
  const load = () => {
    const query = new URLSearchParams({ limit: "25" });
    if (action) query.set("action", action);
    if (actor) query.set("actor", actor);
    void api<AuditRecord[]>(`/api/v1/admin/audit?${query}`).then((result) => {
      setRows(result);
      setHasMoreAudit(result.length === 25);
    });
  };
  useEffect(load, []);
  const loadOlderAudit = async () => {
    if (!rows.length) return;
    const last = rows.at(-1);
    const query = new URLSearchParams({
      limit: "25",
      before: last?.at ?? "",
      before_id: last?.id ?? "",
    });
    if (action) query.set("action", action);
    if (actor) query.set("actor", actor);
    const older = await api<AuditRecord[]>(`/api/v1/admin/audit?${query}`);
    setRows([...rows, ...older]);
    setHasMoreAudit(older.length === 25);
  };
  return (
    <main className="panel glass">
      <h2>Audit</h2>
      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          load();
        }}
      >
        <div className="field">
          <label htmlFor="audit-actor">Actor</label>
          <input
            id="audit-actor"
            value={actor}
            onChange={(event) => setActor(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="audit-action">Action</label>
          <input
            id="audit-action"
            value={action}
            onChange={(event) => setAction(event.target.value)}
          />
        </div>
        <button className="button">Filter</button>
      </form>
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Actor</th>
            <th>Action</th>
            <th>Target</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="mono">{new Date(r.at).toLocaleString()}</td>
              <td>{r.actor}</td>
              <td>{r.action}</td>
              <td className="mono">
                {r.target_type}:{r.target_id}
              </td>
              <td>{r.outcome}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {hasMoreAudit && (
        <button className="button" onClick={() => void loadOlderAudit()}>
          Load older
        </button>
      )}
    </main>
  );
}
export function App() {
  const [me, setMe] = useState<User | null>(null),
    [authError, setAuthError] = useState(""),
    [tab, setTabState] = useState<Tab>(
      () => (location.hash.replace("#admin/", "") as Tab) || "overview",
    );
  useEffect(() => {
    void api<User>("/api/v1/me")
      .then(setMe)
      .catch((e) => setAuthError(String(e)));
  }, []);
  const stream = useEventStream<ConsoleSnapshot>(
    me?.role === "admin" ? "/api/v1/admin/console/events" : "",
    null,
  );
  const setTab = (next: Tab) => {
    history.pushState(null, "", `#admin/${next}`);
    setTabState(next);
  };
  if (authError)
    return (
      <main className="app">
        <p className="error">{authError}</p>
      </main>
    );
  if (!me)
    return (
      <main className="app">
        <p>Authenticating…</p>
      </main>
    );
  if (me.role !== "admin")
    return (
      <main className="app">
        <section className="panel glass">
          <h1>Admin access required</h1>
          <p>Your current role cannot access administrative routes.</p>
        </section>
      </main>
    );
  return (
    <Shell tab={tab} setTab={setTab} snapshot={stream.data}>
      {stream.error && !stream.data ? (
        <p className="error banner">Live state unavailable: {stream.error}</p>
      ) : null}
      {tab === "overview" && stream.data ? (
        <Overview snapshot={stream.data} />
      ) : tab === "overview" ? (
        <p>Connecting to live cluster state…</p>
      ) : tab === "models" ? (
        <DataPage kind="models" />
      ) : tab === "instances" ? (
        <DataPage kind="instances" />
      ) : tab === "identity" ? (
        <IdentityPage />
      ) : tab === "audit" ? (
        <AuditPage />
      ) : (
        <ActivityPage />
      )}
    </Shell>
  );
}
