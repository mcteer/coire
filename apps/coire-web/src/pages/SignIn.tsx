import { useEffect, useState, type FormEvent } from "react";
import { Icon } from "../components/Icons";
import { useTheme } from "../hooks/useTheme";
import { api } from "../api/client";
import type { components } from "../api/schema";

type Status = components["schemas"]["LocalAuthStatus"];
type Session = components["schemas"]["LocalSessionIssued"];

export function SignIn({ onSignedIn }: { onSignedIn: (user: Session["user"]) => void }) {
  const { theme, toggleTheme } = useTheme();
  const [status, setStatus] = useState<Status | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    void api<Status>("/api/v1/auth/local")
      .then(setStatus)
      .catch(() => setError("Cannot reach sign-in. Please reload to try again."));
  }, []);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const session = await api<Session>("/api/v1/auth/local/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      setPassword("");
      onSignedIn(session.user);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-in failed. Please try again.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="signin-page">
      <header className="signin-site-header">
        <a href="/">Coire</a>
      </header>
      <section className="signin-form-region">
        <button
          className="icon-button theme-toggle"
          type="button"
          aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}
          onClick={toggleTheme}
        >
          <Icon name={theme === "light" ? "moon" : "sun"} />
        </button>
        <div className="signin-card">
          <a className="reference-trail" href="/">
            ← Workspace
          </a>
          <h1>Sign in to Coire</h1>
          <p>Continue to your private workspace.</p>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {!status && !error && <p>Loading sign-in…</p>}
          {status?.enabled && (
            <form
              onSubmit={(event) => {
                void submit(event);
              }}
            >
              <label htmlFor="signin-email">Email</label>
              <input
                id="signin-email"
                type="email"
                autoComplete="username"
                required
                maxLength={320}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                disabled={busy}
              />
              <label htmlFor="signin-password">Password</label>
              <input
                id="signin-password"
                type="password"
                autoComplete="current-password"
                required
                maxLength={1024}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={busy}
              />
              <button className="button" type="submit" disabled={busy}>
                {busy ? "Signing in…" : "Sign in"}
                <Icon name="chevron" size={16} />
              </button>
            </form>
          )}
          {status?.access_enabled && (
            <a className="button" href="/">
              Sign in with Access
            </a>
          )}
          {status && !status.enabled && !status.access_enabled && (
            <p>
              Local sign-in has not been configured. The operator must enable it and set your
              account password.
            </p>
          )}
          <div className="signin-note">Use your Coire account credentials to sign in.</div>
        </div>
      </section>
    </main>
  );
}
