import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { KdpsLogo } from "../components/KdpsLogo";
import { apiErrorMessage, authApi } from "../lib/api";
import { ThemeToggle } from "../theme/ThemeToggle";
import "./Login.css";

/** E239 (GSA-T03/ticket 03A): where a temporary-password session lands.
 *
 * The server already confines this session to its own lifecycle and this one
 * write (`PASSWORD_CHANGE_REQUIRED`); this screen is the only room to draw for
 * it. Success ends every session this login holds, this one included, so it
 * signs the person out locally and sends them back to `/login` to prove it. */
export function ChangePassword() {
  const { user, session, loading, completePasswordChange } = useAuth();
  const navigate = useNavigate();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Outside `ProtectedRoute`, so wait for the session the same way it does: a
  // reload here must not bounce a live temporary-password session to sign-in.
  if (loading) {
    return (
      <div className="full-loader">
        <KdpsLogo variant="mark" height={44} title="" />
        <span>Loading KDPS…</span>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  // Nothing to replace: send an ordinary session back into the app rather than
  // showing a screen it has no reason to be on.
  if (session && !session.user.must_change_password) return <Navigate to="/" replace />;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (newPassword !== confirmPassword) {
      setError("The new password and its confirmation do not match.");
      return;
    }
    setBusy(true);
    try {
      await authApi.changePassword(currentPassword, newPassword);
      // The server already ended every session this login holds; match that
      // locally and prove it with a fresh sign-in.
      completePasswordChange();
      navigate("/login");
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <aside className="login-brand">
        <div className="login-brand-inner">
          <KdpsLogo className="login-logo" />
          <h1 className="login-title">Operating System</h1>
        </div>
      </aside>

      <main className="login-panel">
        <div className="login-theme">
          <ThemeToggle compact />
        </div>
        <form className="login-card" onSubmit={submit} data-testid="change-password-form">
          <p className="eyebrow">Replace your password</p>
          <h2 className="h2" style={{ marginBottom: 10 }}>Set your own password</h2>
          <p className="caption" style={{ marginBottom: 18 }}>
            An administrator gave you a temporary password. Replace it with one only you know
            before you can do anything else.
          </p>

          <div className="field" style={{ marginBottom: 14 }}>
            <label htmlFor="current-password">Temporary (or current) password</label>
            <input
              id="current-password"
              type="password"
              autoComplete="current-password"
              className="input"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              autoFocus
              data-testid="current-password"
            />
          </div>
          <div className="field" style={{ marginBottom: 14 }}>
            <label htmlFor="new-password">New password</label>
            <input
              id="new-password"
              type="password"
              autoComplete="new-password"
              className="input"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              data-testid="new-password"
            />
          </div>
          <div className="field" style={{ marginBottom: 18 }}>
            <label htmlFor="confirm-password">Confirm new password</label>
            <input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              className="input"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              data-testid="confirm-password"
            />
          </div>

          {error && (
            <div className="login-error" data-testid="change-password-error">
              {error}
            </div>
          )}

          <button
            className="btn btn-cta btn-block btn-lg"
            disabled={busy}
            data-testid="change-password-submit"
          >
            {busy ? "Replacing…" : "Replace password"}
          </button>
        </form>
      </main>
    </div>
  );
}
