import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, apiErrorMessage } from "../lib/api";
import type { RegistrationState } from "./Signup";
import { useAuth } from "../auth/AuthContext";
import { KdpsLogo } from "../components/KdpsLogo";
import { ThemeToggle } from "../theme/ThemeToggle";
import "./Login.css";

// The six initial roles (RBAC v1, 22 Sep 2026), in the sheet's column order.
// Every other seeded login in `app/test_credentials.md` still signs in by
// typing its email; it just gets no chip here.
const DEMO: { label: string; email: string; password: string }[] = [
  { label: "Owner", email: "owner@kdps.demo", password: "Owner@123" },
  { label: "Store person", email: "deo.manager@kdps.demo", password: "Store@123" },
  { label: "Warehouse", email: "wh.patna@kdps.demo", password: "Wh@123" },
  { label: "Brand manager", email: "brand1@kdps.demo", password: "Brand@123" },
  { label: "Accounts", email: "accounts1@kdps.demo", password: "Acct@123" },
  { label: "Admin", email: "admin@kdps.demo", password: "Admin@123" },
];

export function Login() {
  const { login, sessionExpired, clearSessionExpired, passwordChanged, clearPasswordChanged } =
    useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [registration, setRegistration] = useState<RegistrationState | null>(null);
  useEffect(() => {
    let live = true;
    api
      .get<RegistrationState>("/auth/registration")
      .then(({ data }) => {
        if (live) setRegistration(data);
      })
      .catch(() => {
        if (live) setRegistration(null);
      });
    return () => {
      live = false;
    };
  }, []);

  async function doLogin(u: string, p: string) {
    setError("");
    clearSessionExpired();
    clearPasswordChanged();
    setBusy(true);
    try {
      const { mustChangePassword } = await login(u, p);
      navigate(mustChangePassword ? "/change-password" : "/");
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    void doLogin(email.trim(), password);
  }

  return (
    <div className="login">
      <aside className="login-brand">
        <div className="login-brand-inner">
          {/* The hero stays deep navy in both themes, so the lockup's
              `currentColor` lettering is simply white here. */}
          <KdpsLogo className="login-logo" />
          <h1 className="login-title">Operating System</h1>
        </div>
      </aside>

      <main className="login-panel">
        <div className="login-theme">
          <ThemeToggle compact />
        </div>
        <form className="login-card" onSubmit={submit} data-testid="login-form">
          <p className="eyebrow">Sign in</p>
          <h2 className="h2" style={{ marginBottom: 18 }}>
            Welcome back
          </h2>

          {passwordChanged && (
            <div className="login-success" data-testid="password-changed-note">
              Password changed. Sign in with your new password.
            </div>
          )}

          {sessionExpired && (
            // GSA-T03: authority revoked inside an open journey ends the
            // session at once (server-side); this is the one uniform state
            // that tells the person why they landed back here rather than
            // showing a bare empty form.
            <div className="login-error" data-testid="session-expired-note">
              Your access changed. Sign in again.
            </div>
          )}

          <div className="field" style={{ marginBottom: 14 }}>
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              className="input"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus
              data-testid="login-email"
            />
          </div>
          <div className="field" style={{ marginBottom: 18 }}>
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              className="input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              data-testid="login-password"
            />
          </div>

          {error && (
            <div className="login-error" data-testid="login-error">
              {error}
            </div>
          )}

          <button
            className="btn btn-cta btn-block btn-lg"
            disabled={busy}
            data-testid="login-submit"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>

          {registration?.available && (
            <p>
              <Link to="/signup">Register this company and first store</Link>
            </p>
          )}
          {registration?.synthetic && (
            <div className="login-demo">
              <span>Demo logins</span>
              <div className="login-demo-chips">
                {DEMO.map((d) => (
                  <button
                    key={d.email}
                    type="button"
                    className="chip chip-navy"
                    disabled={busy}
                    onClick={() => {
                      setEmail(d.email);
                      setPassword(d.password);
                      void doLogin(d.email, d.password);
                    }}
                    data-testid={`demo-${d.email.split("@")[0]}`}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </form>
      </main>
    </div>
  );
}
