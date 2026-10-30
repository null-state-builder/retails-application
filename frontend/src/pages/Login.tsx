import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Eye, EyeOff } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import type { DemoLogin, RegistrationState } from "./Signup";
import { useAuth } from "../auth/AuthContext";
import { KdpsLogo } from "../components/KdpsLogo";
import { Note } from "../components/Note";
import { ThemeToggle } from "../theme/ThemeToggle";
import "./Login.css";

// The six initial roles (RBAC v1, 22 Sep 2026), in the sheet's column order,
// as `seed_foundation` creates them. Every other seeded login in
// `app/test_credentials.md` still signs in by typing its email; it just gets no
// chip here. A synthetic tenant built another way (the store test run) sends its
// own logins instead (`KDPS_DEMO_LOGINS`), since these accounts do not exist there.
// Chips are for the local dev server only: a built (deployed) site shows none.
const SHOW_DEMO = import.meta.env.DEV;
const DEMO: DemoLogin[] = [
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
  const [showPassword, setShowPassword] = useState(false);
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
    <SignInFrame>
      <form
        className="login-card"
        onSubmit={submit}
        data-testid="login-form"
        aria-labelledby="login-h"
      >
        <div>
          <h1 className="h1" id="login-h">
            Sign in
          </h1>
          <p className="lead">Use the email and password head office gave you.</p>
        </div>

        {passwordChanged && (
          <Note tone="success" testId="password-changed-note">
            Password changed. Sign in with your new password.
          </Note>
        )}

        {sessionExpired && (
          // GSA-T03: authority revoked inside an open journey ends the
          // session at once (server-side); this is the one uniform state
          // that tells the person why they landed back here rather than
          // showing a bare empty form.
          <Note tone="warn" testId="session-expired-note">
            Your access changed. Sign in again.
          </Note>
        )}

        <div className="field">
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
        <div className="field">
          <label htmlFor="password">Password</label>
          <div className="login-pw">
            <input
              id="password"
              type={showPassword ? "text" : "password"}
              autoComplete="current-password"
              className="input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              data-testid="login-password"
            />
            <button
              type="button"
              className="login-pw-toggle"
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              onClick={() => setShowPassword((shown) => !shown)}
            >
              {showPassword ? (
                <EyeOff size={18} aria-hidden="true" />
              ) : (
                <Eye size={18} aria-hidden="true" />
              )}
            </button>
          </div>
        </div>

        {error && (
          <Note tone="error" testId="login-error">
            {error}
          </Note>
        )}

        <button
          className="btn btn-primary btn-block btn-lg"
          disabled={busy}
          data-testid="login-submit"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="login-help">
          Forgot it? Ask head office for a temporary password; you set your own when you next sign
          in.
        </p>
      </form>

      <Note>
        Signing in needs a connection. Once you are in, the till keeps billing even if the internet
        drops.
      </Note>

      {registration?.available && (
        <p className="login-register">
          <span>New company?</span>
          <Link to="/signup">Register this company and first store</Link>
        </p>
      )}
      {SHOW_DEMO && registration?.synthetic && (
        <div className="login-demo">
          <span>Test server only, demo logins</span>
          <div className="login-demo-chips">
            {(registration.demo_logins?.length ? registration.demo_logins : DEMO).map((d) => (
              <button
                key={d.email}
                type="button"
                className="chip"
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
    </SignInFrame>
  );
}

/** The sign-in screens' frame: the company on the deep navy hero on the left,
 *  and the form on the right with the theme switch above it. Shared
 *  with Change password, which is the same room. */
export function SignInFrame({ children }: { children: ReactNode }) {
  return (
    <div className="login">
      <section className="login-brand" aria-label="KDPS">
        <div className="login-brand-inner">
          {/* The hero stays deep navy in both themes, so the lockup's
              `currentColor` lettering is simply white here. */}
          <KdpsLogo className="login-logo" />
          <p className="login-title">Operating System</p>
        </div>
      </section>

      <main className="login-panel">
        <div className="login-theme">
          <ThemeToggle compact />
        </div>
        <div className="login-stack">{children}</div>
      </main>
    </div>
  );
}
