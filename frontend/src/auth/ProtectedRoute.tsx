import { Suspense, useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";

import { KdpsLogo } from "../components/KdpsLogo";
import { roomAt } from "../routes";
import { AppShell } from "../shell/AppShell";
import { firstDestination, normalizePath, sidebarRows } from "../shell/navConfig";
import { TillProvider } from "../till/TillProvider";
import { AccessDenied } from "./AccessDenied";
import { useAuth } from "./AuthContext";
import { canAccess } from "./routeAccess";

export function ProtectedRoute() {
  const { user, session, loading, refreshSession, featuresOn } = useAuth();
  const location = useLocation();
  // Moving between screens re-reads the session (throttled), so the menu and
  // this guard follow current scoped assignments and policy.
  useEffect(() => {
    refreshSession();
  }, [location.pathname, refreshSession]);
  // The first frame after a refresh is the mark, not bare text: this is the
  // whole screen for as long as the session check takes.
  if (loading) {
    return (
      <div className="full-loader">
        <KdpsLogo variant="mark" height={44} title="" />
        <span>Loading KDPS…</span>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  // GSA-T03/ticket 03A: a temporary-password session can reach only its own
  // session lifecycle and the change-password screen server-side
  // (`PASSWORD_CHANGE_REQUIRED`); send it there before drawing any other room.
  if (session?.user.must_change_password) return <Navigate to="/change-password" replace />;
  const actions = session?.display_actions ?? [];
  const allowed = canAccess(location.pathname, user, actions, featuresOn);
  // A person without Home in server navigation lands on their first authorised
  // screen rather than a blank Home and an otherwise usable menu.
  if (!allowed && normalizePath(location.pathname) === "/") {
    const destination = firstDestination(sidebarRows(user, actions, featuresOn));
    if (destination && normalizePath(destination) !== "/") {
      return <Navigate to={destination} replace />;
    }
  }
  const room = allowed ? roomAt(location.pathname) : undefined;
  const shell = (
    <AppShell {...(room ? { room } : {})}>
      {/* Screens load their code on first open (routes.tsx). One boundary,
          inside the shell: the menu stays put, and a navigation (a
          transition) keeps the current screen until the next one is ready. */}
      <Suspense fallback={<p className="lead">Loading…</p>}>
        {allowed ? <Outlet /> : <AccessDenied />}
      </Suspense>
    </AppShell>
  );
  // The till sits above the shell so the top bar can show its live status. It
  // is always mounted, and only switched on in the counter room: if it came and
  // went with the room, every move between a Sell screen and any other screen
  // would remount the whole shell, and moving between Sell screens would stop
  // and restart the counter (design system v2 rollout, Phase 4).
  return <TillProvider enabled={room === "counter"}>{shell}</TillProvider>;
}
